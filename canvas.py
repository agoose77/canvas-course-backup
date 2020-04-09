# Canvas Backup


import asyncio
import pathlib
import re
import zipfile
from urllib.parse import urljoin, quote, unquote

import aiohttp
import bs4
import slugify
import json
import logging


DOMAIN_URL = "https://canvas.bham.ac.uk"
API_URL = urljoin(DOMAIN_URL, "api/v1/")
JS_WHILE_PREFIX = "while(1);"
pagination_pattern = re.compile(r"<([^>]*)>; rel=\"(\w+)\",?")


logger = logging.getLogger(__name__)


class APIError(Exception):
    pass


async def read_json_response(response):
    data = await response.text()
    if data.startswith(JS_WHILE_PREFIX):
        data = data[len(JS_WHILE_PREFIX) :]

    try:
        payload = json.loads(data)
    except json.JSONDecodeError as err:
        raise APIError(f"Couldn't decode payload {data!r}") from err
    # if "message" in payload:
    #     raise APIError(payload)
    if "errors" in payload:
        raise APIError(payload["errors"])
    return payload


async def request_paginated(s, url, per_page=100):
    while url is not None:
        response = await s.get(url, data={"per_page": per_page})
        results = await read_json_response(response)
        if not isinstance(results, (list, tuple)):
            raise APIError("Paginated result was not iterable")

        for result in results:
            yield result

        pagination = {
            n: u for u, n in pagination_pattern.findall(response.headers["Link"])
        }
        url = pagination.get("next")


async def get_courses(session):
    return [x async for x in request_paginated(session, urljoin(API_URL, "courses"))]


async def wait_for_completion(session, url, sleep=0.1):
    workflow_state = None
    while workflow_state != "completed":
        response = await session.get(url)
        progress = await read_json_response(response)
        workflow_state = progress["workflow_state"]
        await asyncio.sleep(sleep)


async def write_streaming_response(response, file_, chunk_size=4096):
    while True:
        chunk = await response.content.read(chunk_size)
        if not chunk:
            break
        file_.write(chunk)


async def write_course_files(session, course_id, file_, chunk_size=4096, sleep=1.0):
    response = await session.post(
        urljoin(API_URL, f"courses/{course_id}/content_exports"),
        data={"export_type": "zip"},
    )
    export_data = await read_json_response(response)

    # Wait for completion
    await wait_for_completion(session, export_data["progress_url"], sleep=sleep)

    # Get download URL
    export_id = export_data["id"]
    response = await session.get(
        urljoin(API_URL, f"courses/{course_id}/content_exports/{export_id}")
    )
    content_export = await read_json_response(response)

    attachment = content_export["attachment"]
    response = await session.get(attachment["url"])

    await write_streaming_response(response, file_, chunk_size=chunk_size)


async def get_file_table(session: aiohttp.ClientSession, course_id: int):
    responses = request_paginated(
        session, urljoin(API_URL, f"courses/{course_id}/files")
    )
    return {f["id"]: f["display_name"] async for f in responses}


async def get_page(session, course_id, page_url):
    response = await session.get(
        urljoin(API_URL, f"courses/{course_id}/pages/{page_url}")
    )
    return await read_json_response(response)


async def download_course_files(
    session: aiohttp.ClientSession, course_id: int, files_path: pathlib.Path
):
    """Download all course files to disk, if not already present.

    :param session: aiohttp session
    :param course_id: ID of course
    :param files_path: path to files directory
    :return: 
    """

    # Extract files if necessary
    if not files_path.exists():
        download_path = files_path.with_name("download.zip")
        with download_path.open("wb") as f:
            await write_course_files(session, course_id, f)

        zip = zipfile.ZipFile(download_path)
        zip.extractall(files_path)

        download_path.unlink()

    files_path.mkdir(exist_ok=True)


async def resolve_file_id(
    session, course_id: int, file_id: int, file_queue: asyncio.Queue
):
    response = await session.get(
        urljoin(API_URL, f"courses/{course_id}/files/{file_id}")
    )
    file_ = await read_json_response(response)

    # Keep record of file and URL
    await file_queue.put((file_["display_name"], file_['url']))

    return file_["display_name"]


def normalise_page_name(name: str) -> str:
    """Normalise a page name from Canvas to emulate Canvas link resolution.  
    
    :param name: page name
    :return: 
    """
    parts = unquote(name).lower().replace("-", " ").replace("'", "").split()
    return "-".join(parts)


async def download_course_pages(
    session: aiohttp.ClientSession,
    course_id: int,
    course_url: str,
    course_path: pathlib.Path,
    pages_path: pathlib.Path,
    files_path: pathlib.Path,
    file_queue: asyncio.Queue,
):
    """Download all course pages to disk, and rewrite them to support a local server

    :param session: aiohttp session
    :param course_id: ID of course
    :param course_url: local URL of course
    :param course_path: path to course root directory
    :param pages_path: path to pages directory
    :param files_path: path to files directory
    :param file_queue: queue for encountered files
    :return: 
    """
    pages_path.mkdir(exist_ok=True)

    # Remote and local URLS
    remote_course_url = urljoin(DOMAIN_URL, f"courses/{course_id}/")

    pages_url = f"{course_url}{pages_path.relative_to(course_path)}/"
    files_url = f"{course_url}{files_path.relative_to(course_path)}/"

    # Download syllabus
    response = await session.get(
        urljoin(API_URL, f"courses/{course_id}"), data={"include": ["syllabus_body"]}
    )
    course = await read_json_response(response)
    syllabus_body = course["syllabus_body"] or "No syllabus was found."

    # Rewrite syllabus to fix links
    page_html = await create_page_html(
        remote_course_url,
        course_url,
        course["name"],
        syllabus_body,
        pages_url,
        files_url,
        lambda f_id: resolve_file_id(session, course_id, f_id, file_queue),
    )
    index_path = course_path / "index.html"
    index_path.write_text(page_html)

    # Download pages
    responses = request_paginated(
        session, urljoin(API_URL, f"courses/{course_id}/pages")
    )

    try:
        pages = [p async for p in responses]
    except APIError as m:
        logger.exception("Can't get list of pages")
        return
    tasks = [
        asyncio.create_task(get_page(session, course_id, p["url"])) for p in pages
    ]

    # Write pages to disk
    for fut in asyncio.as_completed(tasks):
        page = await fut

        page_html = await create_page_html(
            remote_course_url,
            course_url,
            page["title"],
            page["body"],
            pages_url,
            files_url,
            lambda f_id: resolve_file_id(session, course_id, f_id, file_queue),
        )
        page_file_name = f"{page['url']}.html"
        (pages_path / page_file_name).write_text(page_html)


async def rewrite_page_links(
    soup, remote_course_url: str, pages_url: str, files_url: str, resolve_file_id
):
    remote_pages_url = urljoin(remote_course_url, "pages/")
    remote_files_url = urljoin(remote_course_url, "files/")

    # Pattern to match pages, whilst omitting any parameters
    pattern_pages = re.compile(rf"{remote_pages_url}([^\?]+).*")
    pattern_file = re.compile(rf"{remote_files_url}([^/]+)/([^\?]+).*")

    # Rewrite files
    for tag in "src", "href":
        for el in soup.find_all(attrs={tag: pattern_file}):
            match = pattern_file.match(el[tag])
            file_id = match.group(1)
            file_name = await resolve_file_id(file_id)
            el[tag] = f"{files_url}{quote(file_name)}"

            modifier = match.group(2)
            if modifier == "download":
                el["download"] = file_name

    # Rewrite pages
    for el in soup.find_all(href=pattern_pages):
        page_name = pattern_pages.match(el["href"]).group(1)
        el["href"] = f"{pages_url}{normalise_page_name(page_name)}.html"


async def create_page_html(
    remote_course_url: str,
    course_url: str,
    title: str,
    content: str,
    pages_url: str,
    files_url: str,
    resolve_file_id,
):
    """
    Convert all links to relative links
    Make give .html suffix to pages
    Make files downloadable
    """

    soup = bs4.BeautifulSoup(content, "html.parser")

    # Rewrite page links
    await rewrite_page_links(
        soup, remote_course_url, pages_url, files_url, resolve_file_id
    )

    # Add page title
    title_tag = soup.new_tag("title")
    title_tag.string = title
    soup.insert(0, title_tag)

    # Add controls
    hr_tag = soup.new_tag("hr")
    soup.append(hr_tag)

    links_tag = soup.new_tag("ul")
    soup.append(links_tag)

    li_home_tag = soup.new_tag("li")
    home_tag = soup.new_tag("a", href="/")
    home_tag.string = "List of Courses"
    li_home_tag.append(home_tag)
    links_tag.append(li_home_tag)

    li_course_tag = soup.new_tag("li")
    course_tag = soup.new_tag("a", href=course_url)
    course_tag.string = "This Course"
    li_course_tag.append(course_tag)
    links_tag.append(li_course_tag)

    return soup.prettify(formatter="html")


async def find_missing_files(session, file_queue: asyncio.Queue, files_path: pathlib.Path):
    while not file_queue.empty():
        file_name, file_url = file_queue.get_nowait()
        file_path = files_path / file_name
        print(file_path, file_path.exists())

        if not file_path.exists():
            logger.info(
                f"File {file_name!r} doesn't exist, downloading from {file_url}"
            )

            response = await session.get(file_url)
            with open(file_path, "wb") as f:
                await write_streaming_response(response, f)
        file_queue.task_done()


async def download_course(
    session, downloads_dir, course,
):
    course_id = course["id"]
    course_folder = f"{course['name'].strip()}-{course['id']}"
    course_path = downloads_dir / course_folder
    course_path.mkdir(exist_ok=True)

    # Download files, let task create directory
    files_path = course_path / "files"
    files_task = asyncio.create_task(
        download_course_files(session, course_id, files_path)
    )

    file_queue = asyncio.Queue()

    # Create pages directory
    pages_path = course_path / "pages"
    course_url = f"/{course_folder}/"
    pages_task = asyncio.create_task(
        download_course_pages(
            session,
            course_id,
            course_url,
            course_path,
            pages_path,
            files_path,
            file_queue,
        )
    )

    await asyncio.gather(pages_task, files_task)
    await find_missing_files(session, file_queue, files_path)
