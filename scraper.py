#!/usr/bin/env python3
"""
Sopra Steria .NET Job Scraper
Scrapes job listings from careers.soprasteria.com filtered by .NET keyword
and generates a structured PDF report.
"""

import time
import json
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph,
    Spacer, HRFlowable
)
from reportlab.platypus.flowables import KeepTogether
from reportlab.lib.enums import TA_LEFT, TA_CENTER


BASE_URL = "https://careers.soprasteria.com"
SEARCH_URL = f"{BASE_URL}/jobs"
KEYWORD = ".net"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def fetch_page(page_num: int, retries: int = 3) -> BeautifulSoup | None:
    """Fetch a single search results page."""
    params = {"q": KEYWORD, "options": "", "page": page_num}
    for attempt in range(retries):
        try:
            resp = requests.get(
                SEARCH_URL, params=params, headers=HEADERS, timeout=30
            )
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except requests.RequestException as e:
            print(f"  [page {page_num}] attempt {attempt+1} failed: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


def parse_total_pages(soup: BeautifulSoup) -> int:
    """Extract total number of pages from pagination."""
    # Look for last page link: javascript:pagination(N)
    last_link = soup.select_one(".attrax-pagination__last a")
    if last_link:
        href = last_link.get("href", "")
        m = re.search(r"pagination\((\d+)\)", href)
        if m:
            return int(m.group(1))

    # Fallback: count page items
    page_items = soup.select(".attrax-pagination__page-item a")
    if page_items:
        nums = []
        for a in page_items:
            m = re.search(r"pagination\((\d+)\)", a.get("href", ""))
            if m:
                nums.append(int(m.group(1)))
        if nums:
            return max(nums)

    return 1


def parse_jobs_from_page(soup: BeautifulSoup) -> list[dict]:
    """Extract job listings from a search results page."""
    jobs = []

    # The job cards are typically in list items or article elements
    # Try multiple selectors to find job cards
    selectors = [
        "li[data-id]",
        ".attrax-job-search-result-item",
        "[class*='job-result']",
        "article[class*='job']",
        ".job-list-item",
        "li[class*='job']",
    ]

    cards = []
    for sel in selectors:
        cards = soup.select(sel)
        if cards:
            break

    if not cards:
        # Try to find any anchor with /job/ in href
        links = soup.select("a[href*='/job/'], a[href*='/jobs/']")
        for link in links:
            title = link.get_text(strip=True)
            href = link.get("href", "")
            if title and href:
                full_url = href if href.startswith("http") else BASE_URL + href
                jobs.append({
                    "title": title,
                    "url": full_url,
                    "location": "",
                    "job_type": "",
                    "reference": "",
                    "description": "",
                })
        return jobs

    for card in cards:
        # Title link
        title_el = card.select_one(
            "a[href*='/job/'], a[href*='/jobs/'], h2 a, h3 a, .job-title a, [class*='title'] a"
        )
        if not title_el:
            title_el = card.select_one("h2, h3, [class*='title']")

        if not title_el:
            continue

        title = title_el.get_text(strip=True)
        href = title_el.get("href", "") if title_el.name == "a" else ""
        if not href:
            link = card.select_one("a[href]")
            href = link.get("href", "") if link else ""

        full_url = href if href.startswith("http") else (BASE_URL + href if href else "")

        # Location
        location_el = card.select_one(
            "[class*='location'], [class*='city'], [class*='place'], .location"
        )
        location = location_el.get_text(strip=True) if location_el else ""

        # Job type / category
        type_el = card.select_one(
            "[class*='category'], [class*='job-type'], [class*='contract'], .category"
        )
        job_type = type_el.get_text(strip=True) if type_el else ""

        # Reference/ID
        ref_el = card.select_one("[class*='reference'], [class*='ref'], [data-id]")
        reference = ""
        if ref_el:
            reference = ref_el.get_text(strip=True) or ref_el.get("data-id", "")

        # Short description (if available in listing)
        desc_el = card.select_one(
            "[class*='description'], [class*='summary'], [class*='excerpt'], p"
        )
        description = desc_el.get_text(strip=True) if desc_el else ""

        if title:
            jobs.append({
                "title": title,
                "url": full_url,
                "location": location,
                "job_type": job_type,
                "reference": reference,
                "description": description,
            })

    return jobs


def fetch_job_description(url: str) -> str:
    """Fetch brief description from individual job page."""
    if not url:
        return ""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Try common description containers
        for sel in [
            "[class*='job-description']",
            "[class*='description-content']",
            ".attrax-job-detail",
            "[class*='job-detail']",
            "section[class*='detail']",
            "div[class*='content']",
        ]:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(separator=" ", strip=True)
                if len(text) > 50:
                    return text[:800]  # Limit to 800 chars for PDF

        # Fallback: main content
        main = soup.select_one("main, #main-content, .main-content")
        if main:
            text = main.get_text(separator=" ", strip=True)
            return text[:800]

    except Exception as e:
        print(f"  [fetch desc] {url}: {e}")
    return ""


def scrape_all_jobs(max_pages: int = None) -> list[dict]:
    """Scrape all job listings across all pages."""
    print(f"Fetching page 1 to determine total pages...")
    soup = fetch_page(1)
    if not soup:
        print("ERROR: Could not fetch page 1")
        return []

    total_pages = parse_total_pages(soup)
    if max_pages:
        total_pages = min(total_pages, max_pages)

    print(f"Total pages to scrape: {total_pages}")

    all_jobs = parse_jobs_from_page(soup)
    print(f"  Page 1: {len(all_jobs)} jobs found")

    for page in range(2, total_pages + 1):
        time.sleep(1)  # Be polite
        print(f"  Fetching page {page}/{total_pages}...")
        page_soup = fetch_page(page)
        if page_soup:
            jobs = parse_jobs_from_page(page_soup)
            print(f"  Page {page}: {len(jobs)} jobs found")
            all_jobs.extend(jobs)
        else:
            print(f"  Page {page}: FAILED")

    # Deduplicate by URL
    seen = set()
    unique_jobs = []
    for job in all_jobs:
        key = job["url"] or job["title"]
        if key not in seen:
            seen.add(key)
            unique_jobs.append(job)

    print(f"\nTotal unique jobs scraped: {len(unique_jobs)}")
    return unique_jobs


def build_pdf(jobs: list[dict], output_path: str):
    """Generate a well-structured PDF with job listings table."""
    doc = SimpleDocTemplate(
        output_path,
        pagesize=landscape(A4),
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title="Sopra Steria .NET Job Listings",
        author="Sopra Steria Scraper",
    )

    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        "TitleStyle",
        parent=styles["Title"],
        fontSize=20,
        textColor=colors.HexColor("#1a1a4e"),
        spaceAfter=6,
        alignment=TA_CENTER,
    )
    subtitle_style = ParagraphStyle(
        "SubtitleStyle",
        parent=styles["Normal"],
        fontSize=11,
        textColor=colors.HexColor("#555555"),
        spaceAfter=4,
        alignment=TA_CENTER,
    )
    cell_style = ParagraphStyle(
        "CellStyle",
        parent=styles["Normal"],
        fontSize=8,
        leading=11,
        wordWrap="CJK",
    )
    link_style = ParagraphStyle(
        "LinkStyle",
        parent=styles["Normal"],
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#1a56db"),
        wordWrap="CJK",
    )
    header_style = ParagraphStyle(
        "HeaderStyle",
        parent=styles["Normal"],
        fontSize=9,
        leading=12,
        textColor=colors.white,
        alignment=TA_CENTER,
        fontName="Helvetica-Bold",
    )

    elements = []

    # Title
    elements.append(Paragraph("Sopra Steria — .NET Job Listings", title_style))
    elements.append(Paragraph(
        f"Scraped on {datetime.now().strftime('%B %d, %Y')} · {len(jobs)} positions found",
        subtitle_style
    ))
    elements.append(HRFlowable(
        width="100%", thickness=2,
        color=colors.HexColor("#1a1a4e"), spaceAfter=12
    ))

    # Table header
    col_headers = [
        Paragraph("#", header_style),
        Paragraph("Job Title", header_style),
        Paragraph("Location", header_style),
        Paragraph("Category", header_style),
        Paragraph("Description / Summary", header_style),
        Paragraph("Link", header_style),
    ]

    table_data = [col_headers]

    # Column widths (landscape A4 = ~27.7cm usable)
    page_w = landscape(A4)[0] - 3 * cm
    col_widths = [
        1.0 * cm,   # #
        5.5 * cm,   # title
        3.5 * cm,   # location
        3.5 * cm,   # category
        10.5 * cm,  # description
        3.7 * cm,   # link
    ]

    for i, job in enumerate(jobs, 1):
        title_text = job["title"] or "—"
        location = job["location"] or "—"
        category = job["job_type"] or "—"
        desc = job["description"] or "—"
        if len(desc) > 400:
            desc = desc[:397] + "..."

        url = job["url"]
        if url:
            link_para = Paragraph(
                f'<link href="{url}"><font color="#1a56db">Apply →</font></link>',
                link_style
            )
        else:
            link_para = Paragraph("—", cell_style)

        # Alternate row colors
        row = [
            Paragraph(str(i), cell_style),
            Paragraph(title_text, cell_style),
            Paragraph(location, cell_style),
            Paragraph(category, cell_style),
            Paragraph(desc, cell_style),
            link_para,
        ]
        table_data.append(row)

    table = Table(table_data, colWidths=col_widths, repeatRows=1)

    # Table styling
    header_bg = colors.HexColor("#1a1a4e")
    row_even = colors.HexColor("#f0f4ff")
    row_odd = colors.white
    grid_color = colors.HexColor("#cccccc")

    style_cmds = [
        # Header
        ("BACKGROUND", (0, 0), (-1, 0), header_bg),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ("TOPPADDING", (0, 0), (-1, 0), 8),
        # Body
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("VALIGN", (0, 1), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 1), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        # Grid
        ("GRID", (0, 0), (-1, -1), 0.4, grid_color),
        ("LINEBELOW", (0, 0), (-1, 0), 1.5, header_bg),
    ]

    # Alternating row colors
    for row_idx in range(1, len(table_data)):
        bg = row_even if row_idx % 2 == 0 else row_odd
        style_cmds.append(("BACKGROUND", (0, row_idx), (-1, row_idx), bg))

    table.setStyle(TableStyle(style_cmds))
    elements.append(table)

    # Footer note
    elements.append(Spacer(1, 0.5 * cm))
    elements.append(Paragraph(
        "Note: Click 'Apply →' links to open the full job description on Sopra Steria Careers.",
        ParagraphStyle("FooterNote", parent=styles["Normal"],
                       fontSize=8, textColor=colors.grey, alignment=TA_CENTER)
    ))

    doc.build(elements)
    print(f"\nPDF saved to: {output_path}")


def main():
    print("=" * 60)
    print(" Sopra Steria .NET Job Scraper")
    print("=" * 60)

    # Scrape jobs (set max_pages=None to scrape ALL pages)
    jobs = scrape_all_jobs(max_pages=None)

    if not jobs:
        print("No jobs found. Exiting.")
        return

    # Optionally enrich with job descriptions (slower — fetches each job page)
    # Set to True if you want full descriptions in the PDF
    FETCH_DESCRIPTIONS = False
    if FETCH_DESCRIPTIONS:
        print("\nFetching job descriptions (this may take a while)...")
        for i, job in enumerate(jobs):
            if not job["description"] and job["url"]:
                print(f"  [{i+1}/{len(jobs)}] {job['title'][:50]}...")
                job["description"] = fetch_job_description(job["url"])
                time.sleep(0.8)

    # Save JSON backup
    json_path = "/Users/hamzafaidi/Documents/scrapper-sopra/jobs.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)
    print(f"JSON backup saved to: {json_path}")

    # Build PDF
    pdf_path = "/Users/hamzafaidi/Documents/scrapper-sopra/sopra_dotnet_jobs.pdf"
    print(f"\nGenerating PDF...")
    build_pdf(jobs, pdf_path)


if __name__ == "__main__":
    main()
