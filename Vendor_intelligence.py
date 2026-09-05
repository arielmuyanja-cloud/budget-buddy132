"""
Vendor intelligence engine for Budget Buddy.

This is the layer that turns a messy bank/card line item like
"ADBE*ADOBE 800-833-6687 CA" into a clean, recognized vendor, decides
how confident we are that it's a real recurring subscription, and
attaches cheaper/comparable alternatives. It's the foundation the
audit, confidence scoring, and replacement-engine features sit on top
of — no external services, no new dependencies, runs on the CSV data
Budget Buddy already imports.

Confidence tiers (least to most certain):
  possible          -> matched once, or only a fuzzy match
  confirmed         -> a clear vendor match that billed 2+ times
  agency_verified   -> the agency explicitly confirmed it (still paying
                       for it, or confirmed they cancelled it)
"""
import re
import difflib
from collections import defaultdict

# --- Noise commonly found in bank/card statement descriptions, stripped
# before we try to recognize the vendor underneath it ---
_NOISE_PATTERNS = [
    r"^(pos|debit|purchase|payment to|pmt|ach debit|ach|sq \*|sp \*|paypal \*|tst\*|web pmt)\s+",
    r"\b\d{3}-\d{3}-\d{4}\b",           # phone numbers e.g. 800-833-6687
    r"\b1?8(00|88|77|66|55)\d{7}\b",    # toll-free numbers with no dashes
    r"#\d+",                            # store/reference numbers
    r"\bref\s*#?\s*\w+\b",
    r"\d{1,2}/\d{1,2}(/\d{2,4})?",      # embedded dates
    r"\b[a-z]{2}\b$",                   # trailing 2-letter state codes
]


def clean_description(raw: str) -> str:
    """Strip bank-statement noise so the vendor name underneath is visible."""
    if not raw:
        return ""
    text = raw.strip().lower()
    text = text.replace("*", " ")
    for pat in _NOISE_PATTERNS:
        text = re.sub(pat, " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[^a-z0-9&.\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# --- Known vendor database: canonical id -> display name, matching
# aliases (checked against the cleaned description), category, a
# reference monthly price, and cheaper/comparable alternatives. ---
VENDOR_DB = {
    "adobe": {
        "display_name": "Adobe Creative Cloud", "aliases": ["adobe", "adbe"], "category": "Design",
        "typical_price": 59.99, "alternatives": [
            {"name": "Affinity Suite", "note": "one-time license instead of a subscription", "est_savings_pct": 70},
            {"name": "Canva Pro", "note": "cheaper if the team mainly needs templates/social graphics", "est_savings_pct": 60},
        ]},
    "chatgpt": {
        "display_name": "ChatGPT / OpenAI", "aliases": ["chatgpt", "openai"], "category": "AI Tools",
        "typical_price": 20.0, "alternatives": [
            {"name": "Claude Pro", "note": "comparable pricing, often better for long documents", "est_savings_pct": 0},
        ]},
    "canva": {
        "display_name": "Canva", "aliases": ["canva"], "category": "Design",
        "typical_price": 14.99, "alternatives": []},
    "google_workspace": {
        "display_name": "Google Workspace", "aliases": ["google workspace", "gsuite", "g suite"], "category": "Productivity",
        "typical_price": 12.0, "alternatives": [
            {"name": "Zoho Workplace", "note": "similar suite at a lower per-seat price", "est_savings_pct": 40},
        ]},
    "slack": {
        "display_name": "Slack", "aliases": ["slack"], "category": "Communication",
        "typical_price": 8.75, "alternatives": [
            {"name": "Google Chat (bundled)", "note": "free if already on Google Workspace", "est_savings_pct": 100},
        ]},
    "zoom": {
        "display_name": "Zoom", "aliases": ["zoom"], "category": "Communication",
        "typical_price": 15.99, "alternatives": [
            {"name": "Google Meet (bundled)", "note": "free if already on Google Workspace", "est_savings_pct": 100},
        ]},
    "hubspot": {
        "display_name": "HubSpot", "aliases": ["hubspot"], "category": "CRM/Marketing",
        "typical_price": 45.0, "alternatives": [
            {"name": "Close.com", "note": "leaner CRM at lower cost for small agencies", "est_savings_pct": 30},
        ]},
    "semrush": {
        "display_name": "SEMrush", "aliases": ["semrush"], "category": "SEO",
        "typical_price": 129.95, "alternatives": [
            {"name": "Ahrefs", "note": "overlaps heavily — check you're not paying for both", "est_savings_pct": 100},
        ]},
    "ahrefs": {
        "display_name": "Ahrefs", "aliases": ["ahrefs"], "category": "SEO",
        "typical_price": 129.0, "alternatives": [
            {"name": "SEMrush", "note": "overlaps heavily — check you're not paying for both", "est_savings_pct": 100},
        ]},
    "github": {
        "display_name": "GitHub", "aliases": ["github"], "category": "Dev Tools",
        "typical_price": 4.0, "alternatives": []},
    "render": {
        "display_name": "Render", "aliases": ["render.com", "render"], "category": "Hosting",
        "typical_price": 7.0, "alternatives": []},
    "meta_ads": {
        "display_name": "Meta / Facebook", "aliases": ["meta", "facebook ads", "fb ads"], "category": "Advertising",
        "typical_price": None, "alternatives": []},
    "linkedin": {
        "display_name": "LinkedIn", "aliases": ["linkedin"], "category": "Advertising",
        "typical_price": None, "alternatives": []},
    "figma": {
        "display_name": "Figma", "aliases": ["figma"], "category": "Design",
        "typical_price": 15.0, "alternatives": [
            {"name": "Penpot", "note": "open-source, free self-hosted alternative", "est_savings_pct": 100},
        ]},
    "notion": {
        "display_name": "Notion", "aliases": ["notion"], "category": "Productivity",
        "typical_price": 10.0, "alternatives": [
            {"name": "ClickUp Free tier", "note": "covers docs + tasks for very small teams", "est_savings_pct": 100},
        ]},
    "aws": {
        "display_name": "Amazon Web Services", "aliases": ["aws", "amazon web services"], "category": "Hosting",
        "typical_price": None, "alternatives": []},
    "vercel": {
        "display_name": "Vercel", "aliases": ["vercel"], "category": "Hosting",
        "typical_price": 20.0, "alternatives": []},
    "zapier": {
        "display_name": "Zapier", "aliases": ["zapier"], "category": "Automation",
        "typical_price": 29.99, "alternatives": [
            {"name": "Make.com", "note": "similar automation at a lower entry price", "est_savings_pct": 40},
        ]},
    "dropbox": {
        "display_name": "Dropbox", "aliases": ["dropbox"], "category": "Storage",
        "typical_price": 11.99, "alternatives": [
            {"name": "Google Drive (bundled)", "note": "free if already on Google Workspace", "est_savings_pct": 100},
        ]},
    "mailchimp": {
        "display_name": "Mailchimp", "aliases": ["mailchimp"], "category": "Marketing",
        "typical_price": 20.0, "alternatives": [
            {"name": "MailerLite", "note": "cheaper for small lists with similar features", "est_savings_pct": 50},
        ]},
}

_FUZZY_THRESHOLD = 0.84


def match_vendor(raw_description: str):
    """
    Return (vendor_key, match_confidence, matched_alias) for a transaction
    description, or None if nothing in the vendor database looks like a
    match. match_confidence is 'high' for a direct alias match, 'medium'
    for a fuzzy match.
    """
    cleaned = clean_description(raw_description)
    if not cleaned:
        return None

    for key, info in VENDOR_DB.items():
        for alias in info["aliases"]:
            if alias in cleaned:
                return key, "high", alias

    tokens = cleaned.split()
    candidates = [cleaned] + tokens
    best = None
    for key, info in VENDOR_DB.items():
        for alias in info["aliases"]:
            for cand in candidates:
                ratio = difflib.SequenceMatcher(None, cand, alias).ratio()
                if ratio >= _FUZZY_THRESHOLD and (best is None or ratio > best[3]):
                    best = (key, "medium", alias, ratio)
    if best:
        return best[0], best[1], best[2]
    return None


def build_subscription_report(transactions, verifications=None):
    """
    Group transactions by matched vendor and score confidence.
    verifications: dict of vendor_key -> VendorVerification row (or
    anything with a `.status` attribute) for agency-confirmed vendors.

    Returns a list of dicts sorted by total monthly amount (desc):
      vendor_key, name, category, amount, occurrences, confidence,
      verified_status, alternatives, sample_description, client_name
    """
    verifications = verifications or {}
    groups = defaultdict(list)

    for t in transactions:
        if t.type != "EXPENSE":
            continue
        match = match_vendor(t.description)
        if not match:
            continue
        key, match_confidence, _alias = match
        groups[key].append((t, match_confidence))

    report = []
    for key, entries in groups.items():
        info = VENDOR_DB[key]
        total_amount = sum(t.amount for t, _ in entries)
        occurrences = len(entries)
        latest_tx = max(entries, key=lambda e: e[0].date)[0]
        best_match_conf = "high" if any(c == "high" for _, c in entries) else "medium"

        verified_status = None
        if key in verifications:
            confidence = "agency_verified"
            verified_status = verifications[key].status
        elif best_match_conf == "high" and occurrences >= 2:
            confidence = "confirmed"
        else:
            confidence = "possible"

        report.append({
            "vendor_key": key,
            "name": info["display_name"],
            "category": info["category"],
            "amount": total_amount,
            "occurrences": occurrences,
            "confidence": confidence,
            "verified_status": verified_status,
            "alternatives": info["alternatives"],
            "sample_description": latest_tx.description,
            "client_name": latest_tx.client_name,
        })

    report.sort(key=lambda r: r["amount"], reverse=True)
    return report


def suggest_client_split(vendor_key, transactions, clients):
    """
    Re-billing suggestion for a shared vendor subscription. If past
    transactions for this vendor were already tagged to specific clients,
    use that as evidence; otherwise suggest an even split across all
    of the agency's clients as a starting point for them to confirm.
    Returns a list of {client_name, suggested_pct, evidence}.
    """
    tagged_clients = set()
    for t in transactions:
        if not t.client_name:
            continue
        match = match_vendor(t.description)
        if match and match[0] == vendor_key:
            tagged_clients.add(t.client_name)

    if tagged_clients:
        pct = round(100.0 / len(tagged_clients), 2)
        return [{"client_name": c, "suggested_pct": pct, "evidence": "seen tagged in imported transactions"}
                for c in sorted(tagged_clients)]

    if clients:
        pct = round(100.0 / len(clients), 2)
        return [{"client_name": c.name, "suggested_pct": pct, "evidence": "even split — no usage data yet, confirm with agency"}
                for c in clients]

    return []
