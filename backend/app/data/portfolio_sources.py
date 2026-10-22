"""Where each AMC publishes its monthly portfolio file, and how far automated fetching got (Oct 2026).

Loaded and fetched by hand so far: Nippon India, PPFAS, DSP. Feed a downloaded file to
`uv run python -m app.cli load-portfolios PATH`. Blocked AMCs are never retried or worked around.
"""

PORTFOLIO_SOURCES: dict[str, dict[str, str]] = {
    "HDFC": {
        "page_url": "https://www.hdfcfund.com/statutory-disclosure/portfolio/monthly-portfolio",
        "how_to_fetch": "BLOCKED (Akamai): download by hand.",
    },
    "ICICI Prudential": {
        "page_url": "https://www.icicipruamc.com/media-center/downloads",
        "how_to_fetch": "Direct zip, one workbook per scheme: https://www.icicipruamc.com/blob/downloads/Files/Monthly%20Portfolio%20Disclosures/YYYY/Mon/Monthly-Portfolio-Disclosure-MonthName-YYYY.zip (e.g. 2026/Sep/...-September-2026.zip); load with load-portfolios.",
    },
    "SBI": {
        "page_url": "https://www.sbimf.com/portfolios",
        "how_to_fetch": "Manual: the list is a POST XHR whose URL is not in the page scripts; robots disallow query-string URLs.",
    },
    "Aditya Birla Sun Life": {
        "page_url": "https://mutualfund.adityabirlacapital.com/forms-and-downloads/portfolio",
        "how_to_fetch": "Manual: the page's FactsheetAccordionById XHR returns HTTP 500 to a plain client; download by hand.",
    },
    "Kotak": {
        "page_url": "https://www.kotakmf.com/Information/portfolios",
        "how_to_fetch": "Manual: Angular app, no file list in the HTML; download by hand.",
    },
    "Axis": {
        "page_url": "https://www.axismf.com/statutory-disclosures",
        "how_to_fetch": "Manual: the Portfolios list loads from a separate API not found in the page; download by hand.",
    },
    "Mirae Asset": {
        "page_url": "https://www.miraeassetmf.co.in/downloads/portfolios",
        "how_to_fetch": "Manual: not probed (budget); download by hand.",
    },
    "UTI": {
        "page_url": "https://www.utimf.com/downloads",
        "how_to_fetch": "Manual: JS-rendered page, no file list in the HTML; download by hand.",
    },
    "Motilal Oswal": {
        "page_url": "https://www.motilaloswalmf.com/downloads",
        "how_to_fetch": "Manual: JS-rendered page, no file list in the HTML; download by hand.",
    },
}
