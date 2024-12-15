#!/usr/bin/env python3
# pyright: basic

import argparse
import json
import logging
import pathlib
import sys
import time
from datetime import datetime, UTC
from typing import Dict, List, Optional, Any, Tuple
import requests
from bs4 import BeautifulSoup

class TagFetcher:
    """Handles fetching problem data from Project Euler site."""

    def __init__(self, session_id: str):
        self.session = requests.Session()
        self.base_url = "https://projecteuler.net"
        self.session.cookies.update({
            'PHPSESSID': session_id,
        })
        self.last_csrf_token = None
        self.storage = JsonStorage()

    def get_csrf_token(self, html: str) -> Optional[str]:
        """Extract CSRF token from the search tags form."""
        soup = BeautifulSoup(html, 'html.parser')
        search_form = soup.find('form', {'id': 'search_tags'})
        if search_form:
            csrf_input = search_form.find('input', {'name': 'csrf_token'})
            token = csrf_input['value'] if csrf_input else None
            if token:
                logging.debug(f"Found search form CSRF token: {token[:8]}...")
                return token
            else:
                logging.error("No CSRF token found in search form")
        else:
            logging.error("No search form found in HTML")
        return None

    def get_problem_count(self, html: str) -> int:
        """Get total number of pages from pagination."""
        soup = BeautifulSoup(html, 'html.parser')
        pagination_data = soup.find('script', {'id': 'json_pagination_data'})
        if pagination_data:
            data = json.loads(pagination_data.string)
            pages = data.get('pages', 1)
            logging.debug(f"Found {pages} pages of results")
            return pages
        logging.warning("No pagination data found, defaulting to 1 page")
        return 1

    def get_problems_from_page(self, html: str) -> List[Tuple[str, str, str, int]]:
        """Extract problems from a page."""
        problems = []
        soup = BeautifulSoup(html, 'html.parser')
        table = soup.find('table', {'id': 'problems_table'})
        if table:
            rows = table.find_all('tr')[1:]  # Skip header row
            logging.debug(f"Found {len(rows)} problem rows in table")
            for row in rows:
                cols = row.find_all('td')
                if len(cols) >= 4:  # We need id, title, solved_by, and difficulty
                    problem_id = cols[0].text.strip()
                    problem_link = cols[1].find('a')
                    solved_by = cols[2].find('div', {'class': 'center'})
                    difficulty_div = cols[3].find('div', {'class': 'progress_bar'})

                    if problem_link and solved_by and difficulty_div:
                        title = problem_link.text.strip()
                        solved_count = int(solved_by.text.strip())
                        difficulty = difficulty_div.find('span', {'class': 'tooltiptext_narrow'})
                        difficulty_text = difficulty.text.strip() if difficulty else "Unknown"

                        problems.append((problem_id, title, difficulty_text, solved_count))
                        logging.debug(f"Added problem {problem_id}: {title}")
        else:
            logging.warning("No problems table found in HTML")
        return problems

    def search_tag(self, tag: str) -> List[Tuple[str, str, str, int]]:
        """Search for problems with a specific tag."""
        logging.info(f"Searching for tag: {tag}")

        # Step 1: GET from /archives to get the CSRF token
        if not self.last_csrf_token:
            logging.debug("No existing CSRF token, getting archives page")
            response = self.session.get(f"{self.base_url}/archives")
            if not response.ok:
                msg = f"Failed to get archives page: {response.status_code}"
                logging.error(msg)
                raise ValueError(msg)
            logging.debug(f"Got archives page, status: {response.status_code}")
            self.last_csrf_token = self.get_csrf_token(response.text)
            if not self.last_csrf_token:
                msg = "Could not find CSRF token"
                logging.error(msg)
                raise ValueError(msg)
        else:
            logging.debug(f"Using existing CSRF token: {self.last_csrf_token[:8]}...")

        # Step 2: POST to /search_tags to set up the filter
        search_data = {
            'csrf_token': self.last_csrf_token,
            'search_tags': tag
        }

        search_headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': 'https://projecteuler.net',
            'Referer': 'https://projecteuler.net/archives'
        }

        logging.debug(f"Posting search request for tag '{tag}' with data: {search_data}")
        search_response = self.session.post(
            f"{self.base_url}/search_tags",
            data=search_data,
            headers=search_headers,
            allow_redirects=False  # Don't follow the redirect automatically
        )

        logging.debug(f"Search response status: {search_response.status_code}")
        if search_response.status_code == 200:
            logging.debug(f"Response content: {search_response.text[:200]}...")
            soup = BeautifulSoup(search_response.text, 'html.parser')
            error = soup.find('div', class_='message_body')
            if error:
                logging.error(f"Error message found: {error.text.strip()}")

        if search_response.status_code != 302:
            msg = f"Unexpected response from search: expected 302, got {search_response.status_code}"
            logging.error(msg)
            raise ValueError(msg)

        # Step 3: GET the redirected page to get the filtered results
        response = self.session.get(f"{self.base_url}/archives")
        if not response.ok:
            msg = f"Failed to get results page: {response.status_code}"
            logging.error(msg)
            raise ValueError(msg)

        # Get results from all pages
        all_problems = []
        pages = self.get_problem_count(response.text)
        logging.info(f"Found {pages} pages to process for tag '{tag}'")

        # Process first page that we already have
        problems = self.get_problems_from_page(response.text)
        logging.info(f"Found {len(problems)} problems on page 1")
        all_problems.extend(problems)

        # Get remaining pages if any
        for page in range(2, pages + 1):
            url = f"{self.base_url}/archives;page={page}"
            logging.debug(f"Getting page {page} from {url}")
            response = self.session.get(url)
            if not response.ok:
                logging.warning(f"Failed to get page {page} for tag {tag}: {response.status_code}")
                continue
            problems = self.get_problems_from_page(response.text)
            logging.info(f"Found {len(problems)} problems on page {page}")
            all_problems.extend(problems)
            # if page < pages:  # Don't sleep after the last page
            #     logging.debug("Sleeping for 1 second before next page request")
            #     time.sleep(1)

        logging.info(f"Total problems found for tag '{tag}': {len(all_problems)}")
        return all_problems

    def update_json_data(self, tag: str, problems: List[Tuple[str, str, str, int]]) -> None:
        """Update the JSON data store with new problem information."""
        data = self.storage.load()

        # Update problems dictionary
        for pid, title, difficulty, solved_by in problems:
            if pid not in data['problems']:
                data['problems'][pid] = {
                    'id': pid,
                    'title': title,
                    'url': f'https://projecteuler.net/problem={pid}',
                    'difficulty': difficulty,
                    'solved_by': solved_by
                }
            else:
                # Update potentially changed fields
                data['problems'][pid]['difficulty'] = difficulty
                data['problems'][pid]['solved_by'] = solved_by

        # Update problem_tags dictionary
        for pid, _, _, _ in problems:
            if pid not in data['problem_tags']:
                data['problem_tags'][pid] = [tag]
            elif tag not in data['problem_tags'][pid]:
                data['problem_tags'][pid].append(tag)
                data['problem_tags'][pid].sort()

        # Update tags dictionary
        if tag not in data['tags']:
            data['tags'][tag] = []
        data['tags'][tag] = sorted(list(set(
            data['tags'][tag] + [pid for pid, _, _, _ in problems]
        )))

        # Update metadata
        self.storage.update_metadata(data)

        # Save updated data
        self.storage.save(data)
        logging.info(f"Updated problems.json with data for tag '{tag}'")

    def fetch_tags(self, tags: List[str]) -> bool:
        """Fetch problems for given tags and update problems.json."""
        success = True
        for tag in tags:
            try:
                logging.info(f"Processing tag: {tag}")
                problems = self.search_tag(tag)
                self.update_json_data(tag, problems)
                time.sleep(2)  # Be nice to the server between tags
            except Exception as e:
                logging.error(f"Failed to process tag '{tag}': {e}")
                success = False
                continue
        return success

class JsonStorage:
    """Handles reading and writing the problems.json data store."""

    def __init__(self, file_path: pathlib.Path = pathlib.Path("problems.json")):
        self.file_path = file_path
        self._ensure_file_exists()

    def _ensure_file_exists(self) -> None:
        """Create problems.json with empty structure if it doesn't exist."""
        if not self.file_path.exists():
            initial_data = {
                "metadata": {
                    "last_updated": datetime.now(UTC).isoformat(),
                    "total_problems": 0,
                    "total_tags": 0,
                    "tag_counts": {}
                },
                "problems": {},
                "problem_tags": {},
                "tags": {}
            }
            self.save(initial_data)

    def load(self) -> Dict[str, Any]:
        """Load the JSON data."""
        with open(self.file_path) as f:
            return json.load(f)

    def save(self, data: Dict[str, Any]) -> None:
        """Save the JSON data."""
        with open(self.file_path, 'w') as f:
            json.dump(data, f, indent=2, sort_keys=True)

    def update_metadata(self, data: Dict[str, Any]) -> None:
        """Update the metadata section with current statistics."""
        data['metadata'] = {
            'last_updated': datetime.now(UTC).isoformat(),
            'total_problems': len(data['problems']),
            'total_tags': len(data['tags']),
            'tag_counts': {
                tag: len(problems)
                for tag, problems in data['tags'].items()
            }
        }


class MarkdownGenerator:
    """Generates markdown documentation from problems.json."""

    def __init__(self, output_dir: pathlib.Path):
        self.output_dir = output_dir
        self.problems_dir = output_dir / "problems"
        self.tags_dir = output_dir / "tags"

    def setup_directories(self) -> None:
        """Create necessary directories if they don't exist."""
        self.problems_dir.mkdir(parents=True, exist_ok=True)
        self.tags_dir.mkdir(parents=True, exist_ok=True)

    def slugify(self, text: str) -> str:
        """Convert text to URL-friendly slug."""
        return text.lower().replace(' ', '-')

    def generate_all(self, data: Dict[str, Any]) -> None:
        """Generate all markdown files."""
        logging.info("Generating markdown files")
        self.setup_directories()
        self.generate_main_index(data)
        self.generate_problem_pages(data)
        self.generate_tag_pages(data)


    def validate_problem_data(self, data: Dict[str, Any]) -> None:
        """Validate and fix problem data structure."""
        required_problem_fields = {
            'id': str,
            'title': str,
            'url': str,
            'difficulty': str,
            'solved_by': int
        }

        for pid, problem in list(data['problems'].items()):
            for field, field_type in required_problem_fields.items():
                if field not in problem:
                    logging.warning(f"Problem {pid} missing field '{field}', adding default value")
                    if field_type == str:
                        problem[field] = "Unknown"
                    elif field_type == int:
                        problem[field] = 0

            # Ensure problem has an ID field matching its key
            problem['id'] = pid

            # Ensure URL is present
            if problem['url'] == "Unknown":
                problem['url'] = f"https://projecteuler.net/problem={pid}"

    def format_solved_by(self, count: int) -> str:
        """Format the solved_by count with a fallback for missing data."""
        if count == 0:
            return "Unknown number of"
        return f"{count:,}"

    def format_difficulty(self, difficulty: str) -> str:
        """Format the difficulty with a fallback for missing data."""
        if difficulty == "Unknown":
            return "Unknown difficulty"
        return difficulty

    def generate_problems_index(self, data: Dict[str, Any]) -> None:
        """Generate the problems index page."""
        logging.info("Generating problems index")
        index_path = self.problems_dir / "index.md"

        with open(index_path, 'w') as f:
            f.write("# Project Euler Problems\n\n")
            f.write("[← Main Index](../README.md)\n\n")

            # Sort problems by ID
            sorted_problems = sorted(
                data['problems'].items(),
                key=lambda x: int(x[0])
            )

            for pid, problem in sorted_problems:
                tags = data['problem_tags'].get(pid, [])
                tag_links = [f"[{tag}](../tags/{self.slugify(tag)}.md)" for tag in tags]
                tags_str = ", ".join(tag_links) if tags else "*no tags*"

                f.write(f"## {pid}. {problem['title']}\n\n")
                f.write(f"Tags: {tags_str}\n\n")
                f.write(f"- [Problem Details]({problem['url']})\n")
                f.write(f"- [Local Page]({pid}.md)\n")
                f.write(f"- Solved by {self.format_solved_by(problem['solved_by'])} users\n")
                f.write(f"- Difficulty: {self.format_difficulty(problem['difficulty'])}\n\n")

    def generate_tags_index(self, data: Dict[str, Any]) -> None:
        """Generate the tags index page."""
        logging.info("Generating tags index")
        index_path = self.tags_dir / "index.md"

        with open(index_path, 'w') as f:
            f.write("# Project Euler Tags\n\n")
            f.write("[← Main Index](../README.md)\n\n")

            # Sort tags alphabetically
            sorted_tags = sorted(data['tags'].items())

            for tag, problems in sorted_tags:
                problem_count = len(problems)
                f.write(f"## {tag}\n\n")
                f.write(f"- [{problem_count} problems](tags/{self.slugify(tag)}.md)\n\n")

    def generate_main_index(self, data: Dict[str, Any]) -> None:
        """Generate the main README.md."""
        logging.info("Generating main index")
        index_path = self.output_dir / "README.md"
        metadata = data['metadata']

        with open(index_path, 'w') as f:
            # Header
            f.write("# Project Euler Problems by Tag\n\n")

            # Statistics
            f.write("## Overview\n\n")
            f.write(f"- {metadata['total_problems']} problems\n")
            f.write(f"- {metadata['total_tags']} tags\n\n")

            # Most used tags section
            f.write("## Most Common Tags\n\n")
            sorted_tags = sorted(
                metadata['tag_counts'].items(),
                key=lambda x: (-x[1], x[0])  # Sort by count desc, then name asc
            )[:10]  # Top 10 tags

            for tag, count in sorted_tags:
                f.write(f"- [{tag}](tags/{self.slugify(tag)}.md) ({count} problems)\n")

            # All tags section
            f.write("\n## All Tags\n\n")
            all_tags = sorted(data['tags'].items())

            for tag, problems in all_tags:
                f.write(f"- [{tag}](tags/{self.slugify(tag)}.md) ({len(problems)} problems)\n")

            # Add metadata
            f.write(f"\n\nLast updated: {metadata['last_updated']}\n")

    def generate_problem_pages(self, data: Dict[str, Any], specific_problems: Optional[List[str]] = None) -> None:
        """Generate individual problem pages."""
        problems_to_generate = specific_problems or data['problems'].keys()
        logging.info(f"Generating {len(problems_to_generate)} problem pages")

        for pid in problems_to_generate:
            if pid not in data['problems']:
                logging.warning(f"Problem {pid} not found in data")
                continue

            problem = data['problems'][pid]
            page_path = self.problems_dir / f"{pid}.md"

            with open(page_path, 'w') as f:
                f.write(f"# [{problem['title']}]({problem['url']}) ↗️\n\n")
                f.write(f"{problem['difficulty']}\n")
                f.write(f"Solved by: {self.format_solved_by(problem['solved_by'])} users\n")

                # Add tags section
                tags = data['problem_tags'].get(pid, [])
                if tags:
                    f.write("## Tags\n\n")
                    for tag in sorted(tags):
                        f.write(f"- [{tag}](../tags/{self.slugify(tag)}.md)\n")
                    f.write("\n")

                f.write("\n\n---\n\n")

                f.write("[↑ Main Index](../README.md)\n\n\n")

                # Add next/previous links
                if int(pid) > 1: f.write(f"<div align=center><a href='{int(pid) - 1}.md'>← Previous</a> &nbsp;&nbsp;")
                if int(pid) < len(data['problems']): f.write(f" &nbsp;&nbsp;  <a href='{int(pid) + 1}.md'>Next →</a></div>\n")

    def generate_tag_pages(self, data: Dict[str, Any], specific_tags: Optional[List[str]] = None) -> None:
        """Generate individual tag pages."""
        tags_to_generate = specific_tags or data['tags'].keys()
        logging.info(f"Generating {len(tags_to_generate)} tag pages")

        for tag in tags_to_generate:
            if tag not in data['tags']:
                logging.warning(f"Tag {tag} not found in data")
                continue

            page_path = self.tags_dir / f"{self.slugify(tag)}.md"
            problem_ids = data['tags'][tag]

            with open(page_path, 'w') as f:
                f.write(f"# Problems tagged '{tag}'\n\n")
                f.write("[↑ Main Index](../README.md)\n\n")

                # Add problems list
                problem_count = len(problem_ids)
                f.write(f"{problem_count} problems with this tag:\n\n")

                for pid in sorted(problem_ids, key=int):
                    if pid not in data['problems']:
                        continue
                    problem = data['problems'][pid]
                    f.write(f"- [{pid}. {problem['title']}](../problems/{pid}.md) ")
                    f.write(f"([→ PE]({problem['url']}))\n")

def cmd_fetch_tags(args: argparse.Namespace) -> int:
    """Fetch problems for specified tags."""
    # Get tags from either file or command line
    tags = []
    if args.tags_file:
        with open(args.tags_file) as f:
            tags = [line.strip() for line in f if line.strip()]
    if args.tags:
        tags.extend(args.tags)

    if not tags:
        logging.error("No tags specified. Use --tags-file or --tags")
        return 1

    fetcher = TagFetcher(args.session_id)
    if not fetcher.fetch_tags(tags):
        logging.error("Failed to fetch tag data")
        return 1

    return 0

def cmd_generate_docs(args: argparse.Namespace) -> int:
    """Generate markdown documentation."""
    storage = JsonStorage()
    data = storage.load()

    generator = MarkdownGenerator(pathlib.Path(args.output_dir))

    if args.problems:
        # Generate specific problem pages
        generator.setup_directories()
        generator.generate_problem_pages(data, args.problems)
    elif args.tags:
        # Generate specific tag pages
        generator.setup_directories()
        generator.generate_tag_pages(data, args.tags)
    elif args.indexes:
        # Generate only index files
        generator.setup_directories()
        generator.generate_main_index(data)
        generator.generate_problems_index(data)
        generator.generate_tags_index(data)
    else:
        # Generate everything
        generator.generate_all(data)

    return 0

def main():
    parser = argparse.ArgumentParser(description="Project Euler problem tagger and documentation generator")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # fetch-tags command
    fetch_parser = subparsers.add_parser("fetch-tags", help="Fetch problems for specified tags")
    fetch_parser.add_argument("--session-id", required=True, help="Project Euler session ID")
    fetch_parser.add_argument("--tags-file", default="tags.txt", help="File containing tags, one per line")
    fetch_parser.add_argument("--tags", nargs="+", help="List of tags to fetch")

    # generate-docs command
    gen_parser = subparsers.add_parser("generate-docs", help="Generate markdown documentation")
    gen_parser.add_argument("--output-dir", default=".", help="Output directory for documentation")
    gen_parser.add_argument("--problems", nargs="+", help="Generate only specific problem pages")
    gen_parser.add_argument("--tags", nargs="+", help="Generate only specific tag pages")
    gen_parser.add_argument("--indexes", action="store_true", help="Generate only index files")

    args = parser.parse_args()

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    if not args.command:
        parser.print_help()
        return 1

    # Dispatch to appropriate command
    if args.command == "fetch-tags":
        return cmd_fetch_tags(args)
    elif args.command == "generate-docs":
        return cmd_generate_docs(args)

    return 0

if __name__ == "__main__":
    sys.exit(main())

# #!/usr/bin/env python3
# # pyright: basic
#
# import requests
# from bs4 import BeautifulSoup
# import json
# from collections import defaultdict
# import time
# import argparse
# import logging
# import pathlib
# from typing import Dict, List, Any
#
# class ProjectEulerTagger:
#     def __init__(self, session_id):
#         self.session = requests.Session()
#         self.base_url = "https://projecteuler.net"
#         self.session.cookies.update({
#             'PHPSESSID': session_id,
#         })
#         logging.info(f"Initialized session with PHPSESSID: {session_id[:8]}...")
#         self.last_csrf_token = None
#         self.last_response_text = None
#
#     def get_problem_count(self, html):
#         """Get total number of pages from pagination."""
#         soup = BeautifulSoup(html, 'html.parser')
#         pagination_data = soup.find('script', {'id': 'json_pagination_data'})
#         if pagination_data:
#             data = json.loads(pagination_data.string)
#             pages = data.get('pages', 1)
#             logging.debug(f"Found {pages} pages of results")
#             return pages
#         logging.warning("No pagination data found, defaulting to 1 page")
#         return 1
#
#     def verify_search_tag_set(self, html: str, expected_tag: str) -> bool:
#         """Verify that the search tag input field has the expected value."""
#         soup = BeautifulSoup(html, 'html.parser')
#         tag_input = soup.find('input', {'id': 'tags'})
#         if tag_input and 'value' in tag_input.attrs:
#             actual_tag = tag_input['value']
#             logging.debug(f"Found search tag input with value: '{actual_tag}'")
#             return actual_tag == expected_tag
#         logging.debug("No search tag input found or no value attribute")
#         return False
#
#     def get_csrf_token(self, html):
#         """Extract CSRF token from the search tags form."""
#         soup = BeautifulSoup(html, 'html.parser')
#         search_form = soup.find('form', {'id': 'search_tags'})
#         if search_form:
#             csrf_input = search_form.find('input', {'name': 'csrf_token'})
#             token = csrf_input['value'] if csrf_input else None
#             if token:
#                 logging.debug(f"Found search form CSRF token: {token[:8]}...")
#                 return token
#             else:
#                 logging.error("No CSRF token found in search form")
#         else:
#             logging.error("No search form found in HTML")
#             # Let's also log a snippet of the HTML around where the form should be
#             data_entry = soup.find('div', {'class': 'data_entry'})
#             if data_entry:
#                 logging.debug(f"Data entry div found, contents: {str(data_entry)[:200]}...")
#             else:
#                 logging.debug("No data entry div found in HTML")
#         return None
#
#     def search_tag(self, tag):
#         """Search for problems with a specific tag."""
#         logging.info(f"Searching for tag: {tag}")
#
#         # Step 1: GET from /archives to get the CSRF token
#         if not self.last_csrf_token:
#             logging.debug("No existing CSRF token, getting archives page")
#             response = self.session.get(f"{self.base_url}/archives")
#             if not response.ok:
#                 msg = f"Failed to get archives page: {response.status_code}"
#                 logging.error(msg)
#                 raise ValueError(msg)
#             logging.debug(f"Got archives page, status: {response.status_code}")
#             self.last_csrf_token = self.get_csrf_token(response.text)
#             if not self.last_csrf_token:
#                 msg = "Could not find CSRF token"
#                 logging.error(msg)
#                 raise ValueError(msg)
#         else:
#             logging.debug(f"Using existing CSRF token: {self.last_csrf_token[:8]}...")
#
#         # Step 2: POST to /search_tags to set up the filter
#         search_data = {
#             'csrf_token': self.last_csrf_token,
#             'search_tags': tag
#         }
#
#         search_headers = {
#             'Content-Type': 'application/x-www-form-urlencoded',
#             'Origin': 'https://projecteuler.net',
#             'Referer': 'https://projecteuler.net/archives'
#         }
#
#         logging.debug(f"Posting search request for tag '{tag}' with data: {search_data}")
#         search_response = self.session.post(
#             f"{self.base_url}/search_tags",
#             data=search_data,
#             headers=search_headers,
#             allow_redirects=False  # Don't follow the redirect automatically
#         )
#
#         logging.debug(f"Search response status: {search_response.status_code}")
#         if search_response.status_code == 200:
#             # Let's see what we got back
#             logging.debug(f"Response content: {search_response.text[:200]}...")
#             soup = BeautifulSoup(search_response.text, 'html.parser')
#             error = soup.find('div', class_='message_body')
#             if error:
#                 logging.error(f"Error message found: {error.text.strip()}")
#
#         if search_response.status_code != 302:
#             msg = f"Unexpected response from search: expected 302, got {search_response.status_code}"
#             logging.error(msg)
#             raise ValueError(msg)
#
#         # Step 3: GET the redirected page to get the filtered results
#         response = self.session.get(f"{self.base_url}/archives")
#         if not response.ok:
#             msg = f"Failed to get results page: {response.status_code}"
#             logging.error(msg)
#             raise ValueError(msg)
#
#         # Update the CSRF token for the next request
#         self.last_csrf_token = self.get_csrf_token(response.text)
#
#         # Get results from all pages
#         all_problems = []
#         pages = self.get_problem_count(response.text)
#         logging.info(f"Found {pages} pages to process for tag '{tag}'")
#
#         # Process first page that we already have
#         problems = self.get_problems_from_page(response.text)
#         logging.info(f"Found {len(problems)} problems on page 1")
#         all_problems.extend(problems)
#
#         # Get remaining pages if any
#         for page in range(2, pages + 1):
#             url = f"{self.base_url}/archives;page={page}"
#             logging.debug(f"Getting page {page} from {url}")
#             response = self.session.get(url)
#             if not response.ok:
#                 logging.warning(f"Failed to get page {page} for tag {tag}: {response.status_code}")
#                 continue
#             problems = self.get_problems_from_page(response.text)
#             logging.info(f"Found {len(problems)} problems on page {page}")
#             all_problems.extend(problems)
#             if page < pages:  # Don't sleep after the last page
#                 logging.debug("Sleeping for 1 second before next page request")
#                 time.sleep(1)
#
#         logging.info(f"Total problems found for tag '{tag}': {len(all_problems)}")
#         return all_problems
#
#     def write_tag_file(self, tag: str, problems: list, tag_dir: pathlib.Path) -> None:
#         """Write a markdown file for a specific tag."""
#         tag_filename = tag_dir / f"{tag}.md"
#         logging.info(f"Writing tag file: {tag_filename}")
#
#         # Filter out invalid problem IDs and log them
#         valid_problems = []
#         for pid, title in problems:
#             try:
#                 int(pid)  # Try to convert to int to validate
#                 valid_problems.append((pid, title))
#             except ValueError:
#                 logging.warning(f"Skipping invalid problem ID '{pid}' for tag '{tag}'")
#
#         with open(tag_filename, 'w') as f:
#             f.write(f"# Problems tagged '{tag}'\n\n")
#             f.write("([← Back to index](../README.md))\n\n")
#
#             if valid_problems:
#                 for pid, title in sorted(valid_problems, key=lambda x: int(x[0])):
#                     f.write(f"- [{pid}. {title}](https://projecteuler.net/problem={pid})\n")
#             else:
#                 f.write("*No valid problems found for this tag.*\n")
#
#             f.write("\n")
#             f.write("---\n")
#             f.write("([← Back to index](../README.md))\n")
#
#     def get_problems_from_page(self, html):
#         """Extract problems from a page."""
#         problems = []
#         soup = BeautifulSoup(html, 'html.parser')
#         table = soup.find('table', {'id': 'problems_table'})
#         if table:
#             rows = table.find_all('tr')[1:]  # Skip header row
#             logging.debug(f"Found {len(rows)} problem rows in table")
#             for row in rows:
#                 cols = row.find_all('td')
#                 if len(cols) >= 2:
#                     problem_id = cols[0].text.strip()
#                     problem_link = cols[1].find('a')
#                     if problem_link and problem_id:  # Only add if we have both ID and title
#                         title = problem_link.text.strip()
#                         problems.append((problem_id, title))
#                         logging.debug(f"Added problem {problem_id}: {title}")
#         else:
#             logging.warning("No problems table found in HTML")
#         return problems
#
#     def generate_markdown(self, tags_file: str, output_dir: str) -> None:
#         """Generate markdown files for each tag and an index, plus JSON data."""
#         logging.info(f"Reading tags from {tags_file}")
#         problems_by_tag = defaultdict(list)
#
#         # Track all unique problems for JSON output
#         all_problems: Dict[str, Dict[str, Any]] = {}
#         problem_tags: Dict[str, List[str]] = defaultdict(list)
#
#         # Create output directory structure
#         output_path = pathlib.Path(output_dir)
#         tag_dir = output_path / "tags"
#         tag_dir.mkdir(parents=True, exist_ok=True)
#         logging.info(f"Created output directory structure in {output_path}")
#
#         # Read tags and search for problems
#         with open(tags_file, 'r') as f:
#             tags = [line.strip() for line in f if line.strip()]
#         logging.info(f"Found {len(tags)} tags to process")
#
#         # Process each tag
#         for i, tag in enumerate(tags, 1):
#             logging.info(f"Processing tag {i}/{len(tags)}: {tag}")
#             try:
#                 problems = self.search_tag(tag)
#                 if problems:
#                     # Filter out invalid problem IDs
#                     valid_problems = []
#                     for pid, title in problems:
#                         try:
#                             if pid:  # Check for empty string
#                                 int(pid)  # Validate ID can be converted to int
#                                 valid_problems.append((pid, title))
#                             else:
#                                 logging.warning(f"Empty problem ID found for tag '{tag}'")
#                         except ValueError:
#                             logging.warning(f"Invalid problem ID '{pid}' found for tag '{tag}'")
#
#                     if valid_problems:
#                         # Store the valid problems for this tag
#                         problems_by_tag[tag] = valid_problems
#
#                         # Update the problem tracking dictionaries
#                         for pid, title in valid_problems:
#                             if pid not in all_problems:
#                                 all_problems[pid] = {
#                                     "id": pid,
#                                     "title": title,
#                                     "url": f"https://projecteuler.net/problem={pid}"
#                                 }
#                             problem_tags[pid].append(tag)
#
#                         # Write the tag's markdown file immediately
#                         self.write_tag_file(tag, valid_problems, tag_dir)
#                         logging.info(f"Wrote markdown file for tag '{tag}' with {len(valid_problems)} valid problems")
#                     else:
#                         logging.warning(f"No valid problems found for tag '{tag}' after filtering")
#                 else:
#                     logging.warning(f"No problems found for tag '{tag}'")
#
#                 if i < len(tags):  # Don't sleep after the last tag
#                     logging.debug("Sleeping for 2 seconds before next tag")
#                     time.sleep(2)
#             except ValueError as e:
#                 logging.error(f"Error processing tag {tag}: {str(e)}")
#                 continue  # Continue with next tag even if this one fails
#
#         # Save JSON data
#         json_data = {
#             "problems": all_problems,
#             "problem_tags": problem_tags,
#             "tags": {
#                 tag: [p[0] for p in problems]
#                 for tag, problems in problems_by_tag.items()
#             }
#         }
#
#         json_path = output_path / "problems.json"
#         logging.info(f"Writing JSON data to {json_path}")
#         with open(json_path, 'w') as f:
#             json.dump(json_data, f, indent=2, sort_keys=True)
#
#         # Generate main index file
#         logging.info("Writing main index file")
#         with open(output_path / "README.md", 'w') as f:
#             f.write("# Project Euler Problems by Tag\n\n")
#             f.write("Index of problem categories:\n\n")
#
#             # Write sorted index with problem counts
#             for tag in sorted(problems_by_tag.keys()):
#                 problems = problems_by_tag[tag]
#                 if problems:
#                     problem_count = len(problems)
#                     f.write(f"- [{tag}](tags/{tag}.md) ({problem_count} problems)\n")
#
#             # Add summary
#             f.write(f"\n## Summary\n\n")
#             total_problems = len(all_problems)
#             total_tags = len(problems_by_tag)
#             f.write(f"- Unique problems: {total_problems}\n")
#             f.write(f"- Total tags: {total_tags}\n")
#
#             # Add info about raw data
#             f.write("\n## Data\n\n")
#             f.write("Raw data is available in [problems.json](problems.json) with the following structure:\n")
#             f.write("```jsonp\n")
#             f.write("{\n")
#             f.write('  "problems": {      // All unique problems with their details\n')
#             f.write('    "1": {\n')
#             f.write('      "id": "1",\n')
#             f.write('      "title": "Problem title",\n')
#             f.write('      "url": "https://projecteuler.net/problem=1"\n')
#             f.write('    },\n')
#             f.write('    ...\n')
#             f.write('  },\n')
#             f.write('  "problem_tags": {  // Problems with their associated tags\n')
#             f.write('    "1": ["tag1", "tag2"],\n')
#             f.write('    ...\n')
#             f.write('  },\n')
#             f.write('  "tags": {          // Tags with their associated problems\n')
#             f.write('    "tag1": ["1", "2", "3"],\n')
#             f.write('    ...\n')
#             f.write('  }\n')
#             f.write('}\n')
#             f.write('```\n')
#
#             # Note about multiple tags
#             f.write("\nNote: Problems may appear in multiple tags.\n")
#
# def main():
#     parser = argparse.ArgumentParser(description='Generate markdown files of Project Euler problems organized by tags')
#     parser.add_argument('--session-id', required=True, help='PHPSESSID cookie value')
#     parser.add_argument('--tags-file', default='tags.txt', help='File containing tags, one per line')
#     parser.add_argument('--output', default='.', help='Output directory for markdown files')
#     parser.add_argument('--debug', action='store_true', help='Enable debug logging')
#
#     args = parser.parse_args()
#
#     # Configure logging
#     logging.basicConfig(
#         level=logging.DEBUG if args.debug else logging.INFO,
#         format='%(asctime)s - %(levelname)s - %(message)s',
#         datefmt='%Y-%m-%d %H:%M:%S'
#     )
#
#     logging.info("Starting Project Euler problem tagging")
#     tagger = ProjectEulerTagger(args.session_id)
#     tagger.generate_markdown(args.tags_file, args.output)
#     logging.info("Finished processing")
#
# if __name__ == "__main__":
#     main()
