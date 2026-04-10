#!/usr/bin/env python3
"""
ILS Auto-Quote Engine for Hermes Gateway
=========================================
Deterministic pipeline: ILS RFQ -> V11 stock check -> draft quote -> notification.

No AI involved. Pure data pipeline.
"""
import base64
import json
import logging
import os
import sqlite3
import tempfile
import xmlrpc.client
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional, Tuple

log = logging.getLogger("ils_auto_quote")

# ============================================================================
# Configuration
# ============================================================================

HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
DATA_DIR = HERMES_HOME / "services" / "data"
DB_PATH = DATA_DIR / "ils_quotes.db"

# V11 connection
V11_URL = os.getenv("ODOO_URL", "https://v11.advanced.aero")
V11_DB = os.getenv("ODOO_DB", "advancedaero")
V11_USER = os.getenv("ODOO_USER", "ac@advanced.aero")
V11_PASS = os.getenv("ODOO_PASSWORD", "")

# ILS connection
ILS_USER = os.getenv("ILS_USER", "")
ILS_PASS = os.getenv("ILS_PASSWORD", "")

# Pricing defaults
DEFAULT_MARKUP = 2.0  # 100% markup on cost when no history
IDG_MARKUP = 1.65     # 65% markup for IDG family parts (76xxxx, 75xxxx)
IDG_PREFIXES = ('76', '75', '77', '59')  # IDG/CSD family prefixes
MIN_AUTO_QUOTE_PRICE = 50.0  # Below $50 = noise, flag for manual review
PLACEHOLDER_COSTS = {0, 0.0, 1, 1.0}  # V11 default/placeholder list_price values


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class RFQPart:
    """A single part from an ILS RFQ."""
    part_number: str
    quantity: int = 1
    description: str = ""


@dataclass
class StockMatch:
    """A part matched against V11 inventory."""
    part_number: str
    product_id: int
    template_id: int
    qty_available: float
    condition: str = "AR"
    unit_cost: float = 0.0
    suggested_price: float = 0.0
    description: str = ""
    lot_id: Optional[int] = None
    lot_name: Optional[str] = None
    pricing_method: str = "NEEDS_MANUAL"
    # Context fields for manual pricing
    platform: str = ""
    oem: str = ""
    is_idg: bool = False
    confidence: int = 0


@dataclass
class ILSRfq:
    """Parsed ILS RFQ."""
    rfq_id: str
    company: str
    contact_name: str
    contact_email: str
    parts: List[RFQPart] = field(default_factory=list)
    received_at: str = ""
    contact_phone: str = ""
    contact_title: str = ""
    ils_company_id: str = ""
    raw: Dict = field(default_factory=dict)

    @property
    def is_priority(self) -> bool:
        """Check if any part is in IDG family."""
        return any(
            p.part_number.startswith(IDG_PREFIXES)
            for p in self.parts
        )


@dataclass
class QuoteResult:
    """Result of creating a draft quote in V11."""
    success: bool
    order_id: Optional[int] = None
    order_name: Optional[str] = None
    partner_id: Optional[int] = None
    partner_name: Optional[str] = None
    total_amount: float = 0.0
    matched_parts: List[StockMatch] = field(default_factory=list)
    unmatched_parts: List[str] = field(default_factory=list)
    rfq: Optional[ILSRfq] = None
    error: Optional[str] = None
    created_customer: bool = False


# ============================================================================
# V11 Client (XML-RPC)
# ============================================================================

class V11Client:
    """Lightweight V11 XML-RPC client for quoting operations."""

    def __init__(self):
        self.url = V11_URL
        self.db = V11_DB
        self.username = V11_USER
        self.password = V11_PASS
        self.uid = None
        self._common = None
        self._models = None

    @property
    def common(self):
        if self._common is None:
            self._common = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/common')
        return self._common

    @property
    def models(self):
        if self._models is None:
            self._models = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/object')
        return self._models

    def authenticate(self) -> bool:
        try:
            self.uid = self.common.authenticate(self.db, self.username, self.password, {})
            return bool(self.uid)
        except Exception as e:
            log.error("V11 auth failed: %s", e)
            return False

    def execute(self, model: str, method: str, *args, **kwargs):
        if not self.uid and not self.authenticate():
            raise ConnectionError("V11 authentication failed")
        try:
            return self.models.execute_kw(
                self.db, self.uid, self.password,
                model, method, list(args), kwargs
            )
        except xmlrpc.client.Fault as e:
            # Session expired — re-authenticate once and retry
            if 'AccessDenied' in str(e) or 'denied' in str(e).lower():
                log.warning("V11 session expired, re-authenticating...")
                self.uid = None
                if self.authenticate():
                    return self.models.execute_kw(
                        self.db, self.uid, self.password,
                        model, method, list(args), kwargs
                    )
            raise

    def search_read(self, model, domain, fields, limit=100, order=None):
        kw = {'fields': fields, 'limit': limit}
        if order:
            kw['order'] = order
        return self.execute(model, 'search_read', domain, **kw)

    # --- Part Operations ---

    def find_part(self, part_number: str) -> Optional[Dict]:
        """Find a part by part number. V11 uses product.template.name for PN."""
        # Search product.product by name (which maps to template name)
        products = self.search_read(
            'product.product',
            [['name', '=', part_number]],
            ['id', 'name', 'qty_available', 'product_tmpl_id', 'lst_price'],
            limit=5
        )
        if not products:
            # Try ilike for partial matches
            products = self.search_read(
                'product.product',
                [['name', 'ilike', part_number]],
                ['id', 'name', 'qty_available', 'product_tmpl_id', 'lst_price'],
                limit=5
            )
        # Filter to those with stock
        in_stock = [p for p in products if p.get('qty_available', 0) > 0]
        return in_stock[0] if in_stock else (products[0] if products else None)

    def get_template_info(self, template_id: int) -> Dict:
        """Get product template details (pricing, description)."""
        templates = self.execute(
            'product.template', 'read', [template_id],
            fields=['list_price', 'description_sale', 'description', 'name']
        )
        return templates[0] if templates else {}

    def get_sale_history(self, part_number: str, customer_id: Optional[int] = None) -> List[Dict]:
        """Get historical sale prices for a part, optionally filtered by customer."""
        domain = [
            ['product_id.name', '=', part_number],
            ['order_id.state', 'in', ['sale', 'done']],
        ]
        if customer_id:
            domain.append(['order_id.partner_id', '=', customer_id])

        lines = self.search_read(
            'sale.order.line',
            domain,
            ['price_unit', 'product_uom_qty', 'order_id', 'create_date'],
            limit=20,
            order='create_date desc'
        )
        return lines

    # --- Customer Operations ---

    def find_customer_by_email(self, email: str) -> Optional[Dict]:
        """Find customer by exact email match."""
        partners = self.search_read(
            'res.partner',
            [['email', '=', email]],
            ['id', 'name', 'email', 'phone', 'is_company'],
            limit=1
        )
        return partners[0] if partners else None

    def find_customer_by_name(self, name: str) -> Optional[Dict]:
        """Find customer by fuzzy name match."""
        partners = self.search_read(
            'res.partner',
            [['name', 'ilike', name], ['customer', '=', True]],
            ['id', 'name', 'email', 'phone', 'is_company'],
            limit=5
        )
        return partners[0] if partners else None

    def find_customer_by_domain(self, domain: str) -> Optional[Dict]:
        """Find customer by email domain."""
        partners = self.search_read(
            'res.partner',
            [['email', 'ilike', f'%@{domain}'], ['customer', '=', True]],
            ['id', 'name', 'email', 'phone', 'is_company'],
            limit=1
        )
        return partners[0] if partners else None

    def create_customer(self, name: str, email: str, company: str = "") -> int:
        """Create a new customer in V11. Returns partner_id."""
        vals = {
            'name': company or name,
            'email': email,
            'customer': True,
            'is_company': bool(company),
        }
        if company and name != company:
            # Create company first, then contact as child
            company_id = self.execute('res.partner', 'create', vals)
            # Create contact under company
            contact_vals = {
                'name': name,
                'email': email,
                'parent_id': company_id,
                'type': 'contact',
            }
            self.execute('res.partner', 'create', contact_vals)
            return company_id
        return self.execute('res.partner', 'create', vals)

    def find_or_create_customer(self, name: str, email: str, company: str = "") -> Tuple[int, str, bool]:
        """
        Find or create customer. Returns (partner_id, partner_name, was_created).

        Search order:
        1. Exact email match
        2. Email domain match
        3. Fuzzy company name match
        4. Create new
        """
        # 1. Exact email
        partner = self.find_customer_by_email(email)
        if partner:
            return partner['id'], partner['name'], False

        # 2. Domain match
        if '@' in email:
            domain = email.split('@')[1]
            partner = self.find_customer_by_domain(domain)
            if partner:
                return partner['id'], partner['name'], False

        # 3. Fuzzy company name
        if company:
            partner = self.find_customer_by_name(company)
            if partner:
                return partner['id'], partner['name'], False

        # 4. Create new
        partner_id = self.create_customer(name, email, company)
        partner_name = company or name
        log.info("Created V11 customer: %s (id=%d)", partner_name, partner_id)
        return partner_id, partner_name, True

    # --- Quote Operations ---

    def create_draft_quote(self, partner_id: int, lines: List[Dict], note: str = "") -> Dict:
        """
        Create a draft sale.order in V11.

        lines: [{product_id: int, quantity: float, price_unit: float}]
        Returns: {id: int, name: str}
        """
        order_lines = []
        for line in lines:
            line_vals = {
                'product_id': line['product_id'],
                'product_uom_qty': line.get('quantity', 1),
                'price_unit': line['price_unit'],
            }
            if line.get('condition'):
                line_vals['x_studio_field_cOZEb'] = line['condition']
            order_lines.append((0, 0, line_vals))

        order_id = self.execute('sale.order', 'create', {
            'partner_id': partner_id,
            'order_line': order_lines,
            'note': note,
        })

        # Read back the order name
        order = self.execute('sale.order', 'read', [order_id], fields=['name'])
        order_name = order[0]['name'] if order else f"SO-{order_id}"

        return {'id': order_id, 'name': order_name}

    def cancel_draft_quote(self, order_id: int) -> bool:
        """Cancel a draft quote."""
        try:
            self.execute('sale.order', 'action_cancel', [order_id])
            return True
        except Exception as e:
            log.error("Failed to cancel SO %d: %s", order_id, e)
            return False

    def get_quote_pdf(self, order_id: int, order_name: str = "") -> Optional[Path]:
        """
        Download the quote PDF from V11 via HTTP session auth.

        Uses the same report template as the Odoo Print button:
        /report/pdf/sale.report_saleorder/{order_id}

        Returns: Path to temp PDF file, or None on failure.
        """
        import requests

        try:
            # Authenticate via JSON-RPC to get session cookie
            session = requests.Session()
            auth_resp = session.post(
                f"{self.url}/web/session/authenticate",
                json={
                    "jsonrpc": "2.0",
                    "params": {
                        "db": self.db,
                        "login": self.username,
                        "password": self.password,
                    },
                },
                timeout=15,
            )
            auth_data = auth_resp.json()
            if auth_data.get("error"):
                log.error("V11 session auth failed: %s", auth_data["error"])
                return None

            # Fetch the PDF report (AAC custom quote form)
            pdf_resp = session.get(
                f"{self.url}/report/pdf/sale.report_saleorder/{order_id}",
                timeout=30,
            )
            if pdf_resp.status_code != 200 or not pdf_resp.content:
                log.error("V11 PDF fetch failed: HTTP %d", pdf_resp.status_code)
                return None

            # Verify it's actually a PDF
            if not pdf_resp.content[:5].startswith(b"%PDF"):
                log.error("V11 returned non-PDF content for SO %d", order_id)
                return None

            # Write to temp file
            filename = order_name or f"SO-{order_id}"
            filename = filename.replace("/", "-")
            pdf_dir = DATA_DIR / "pdfs"
            pdf_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = pdf_dir / f"Quotation-{filename}.pdf"
            pdf_path.write_bytes(pdf_resp.content)

            log.info("Downloaded quote PDF: %s (%d bytes)", pdf_path.name, len(pdf_resp.content))
            return pdf_path

        except Exception as e:
            log.error("Failed to download quote PDF for SO %d: %s", order_id, e)
            return None


# ============================================================================
# ILS Client (SOAP)
# ============================================================================

class ILSClient:
    """ILS SOAP API client for RFQ retrieval."""

    def __init__(self):
        self.user_id = ILS_USER
        self.password = ILS_PASS
        self._client_v3 = None
        self._client_v2 = None

    @property
    def client_v3(self):
        if self._client_v3 is None:
            from zeep import Client
            self._client_v3 = Client("https://secure.ilsmart.com/services/v3/soap11?wsdl")
        return self._client_v3

    @property
    def client_v2(self):
        if self._client_v2 is None:
            from zeep import Client
            self._client_v2 = Client("https://secure.ilsmart.com/services/v2/soap11?wsdl")
        return self._client_v2

    def get_new_rfqs(self) -> List[ILSRfq]:
        """Poll ILS for new RFQs not yet retrieved.

        Raises on network/DNS failures so the caller can distinguish
        "no new RFQs" (empty list) from "poll failed" (exception).
        """
        from zeep.helpers import serialize_object
        result = self.client_v3.service.GetNewRfqsReceived(
            UserId=self.user_id,
            Password=self.password,
            IncludeClosed=False
        )
        raw = serialize_object(result)
        return self._parse_rfqs(raw)

    def _parse_rfqs(self, raw: Dict) -> List[ILSRfq]:
        """Parse ILS SOAP response into ILSRfq objects.

        Handles the v3 SOAP structure:
          Rfqs.Rfq[].ContactInfo.rfqSender.{CompanyName, IlsContact.{Name, Email}}
          Rfqs.Rfq[].RequestedItems.Item.RfqRequestedPart[] (list of parts)
        """
        rfqs_container = raw.get('Rfqs')
        if not rfqs_container:
            return []

        rfq_data = rfqs_container.get('Rfq', [])
        if not rfq_data:
            return []
        if not isinstance(rfq_data, list):
            rfq_data = [rfq_data]

        parsed = []
        for r in rfq_data:
            parts = []
            items = r.get('RequestedItems', {}) or {}
            if items:
                item_container = items.get('Item', {})
                # Item can be a dict (single item) or list (multiple items)
                if isinstance(item_container, dict):
                    item_list = [item_container]
                elif isinstance(item_container, list):
                    item_list = item_container
                else:
                    item_list = []

                for item in item_list:
                    # RfqRequestedPart can be a list of parts or a single dict
                    rfq_parts_raw = item.get('RfqRequestedPart', []) or []
                    if isinstance(rfq_parts_raw, dict):
                        rfq_parts_raw = [rfq_parts_raw]

                    for rfq_part in rfq_parts_raw:
                        if not isinstance(rfq_part, dict):
                            continue
                        pn = rfq_part.get('PartNumber', '') or ''
                        qty = int(rfq_part.get('Quantity', 1) or 1)
                        desc = rfq_part.get('Description', '') or rfq_part.get('PartDescription', '') or ''
                        if pn:
                            parts.append(RFQPart(
                                part_number=pn.strip(),
                                quantity=qty,
                                description=desc,
                            ))

            # Extract company/contact from ContactInfo.rfqSender (v3 structure)
            contact_info_wrapper = r.get('ContactInfo', {}) or {}
            sender = contact_info_wrapper.get('rfqSender', {}) or {}
            ils_contact = sender.get('IlsContact', {}) or {}

            company = sender.get('CompanyName', '') or r.get('CompanyName', '') or ''
            contact_name = ils_contact.get('Name', '') or r.get('ContactName', '') or ''
            contact_email = ils_contact.get('Email', '') or r.get('Email', '') or ''
            contact_title = ils_contact.get('Title', '') or ''
            ils_company_id = ils_contact.get('CompanyId', '') or sender.get('CompanyId', '') or ''

            # Extract phone number
            phone_obj = ils_contact.get('Phone', {}) or {}
            contact_phone = ''
            if isinstance(phone_obj, dict):
                phone_num = phone_obj.get('PhoneNumber', '') or ''
                country_code = phone_obj.get('CountryCallingCode', '') or ''
                ext = phone_obj.get('Extension', '') or ''
                if phone_num:
                    contact_phone = f"+{country_code}{phone_num}" if country_code else phone_num
                    if ext:
                        contact_phone += f" x{ext}"

            parsed.append(ILSRfq(
                rfq_id=str(r.get('RfqId', '')),
                company=company,
                contact_name=contact_name,
                contact_email=contact_email,
                parts=parts,
                received_at=str(r.get('CreateDate', '') or r.get('ReceivedDate', '')),
                contact_phone=contact_phone,
                contact_title=contact_title,
                ils_company_id=ils_company_id,
                raw=r,
            ))
        return parsed


# ============================================================================
# Quote Database (SQLite tracking)
# ============================================================================

class QuoteDB:
    """Track pending quotes for approval workflow."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS quotes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rfq_id TEXT NOT NULL,
                    order_id INTEGER,
                    order_name TEXT,
                    partner_id INTEGER,
                    company TEXT,
                    contact_email TEXT,
                    contact_name TEXT,
                    parts_json TEXT,
                    total_amount REAL DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    telegram_msg_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP,
                    UNIQUE(rfq_id)
                )
            ''')
            conn.execute('''
                CREATE TABLE IF NOT EXISTS seen_rfqs (
                    rfq_id TEXT PRIMARY KEY,
                    seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            conn.commit()

    def is_rfq_seen(self, rfq_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_rfqs WHERE rfq_id = ?", (rfq_id,)
            ).fetchone()
            return row is not None

    def mark_rfq_seen(self, rfq_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO seen_rfqs (rfq_id) VALUES (?)", (rfq_id,)
            )
            conn.commit()

    def save_quote(self, rfq_id: str, order_id: int, order_name: str,
                   partner_id: int, company: str, contact_email: str,
                   contact_name: str, parts: List[Dict], total_amount: float,
                   telegram_msg_id: int = None) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('''
                INSERT OR REPLACE INTO quotes
                (rfq_id, order_id, order_name, partner_id, company,
                 contact_email, contact_name, parts_json, total_amount,
                 status, telegram_msg_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            ''', (
                rfq_id, order_id, order_name, partner_id, company,
                contact_email, contact_name, json.dumps(parts),
                total_amount, telegram_msg_id
            ))
            conn.commit()
            return cursor.lastrowid

    def get_quote(self, quote_id: int) -> Optional[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM quotes WHERE id = ?", (quote_id,)
            ).fetchone()
            if row:
                d = dict(row)
                d['parts'] = json.loads(d.get('parts_json', '[]'))
                return d
            return None

    def get_quote_by_rfq(self, rfq_id: str) -> Optional[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM quotes WHERE rfq_id = ?", (rfq_id,)
            ).fetchone()
            if row:
                d = dict(row)
                d['parts'] = json.loads(d.get('parts_json', '[]'))
                return d
            return None

    def update_status(self, quote_id: int, status: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE quotes SET status = ?, resolved_at = ? WHERE id = ?",
                (status, datetime.now(tz=timezone.utc).isoformat(), quote_id)
            )
            conn.commit()

    def update_telegram_msg(self, quote_id: int, msg_id: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE quotes SET telegram_msg_id = ? WHERE id = ?",
                (msg_id, quote_id)
            )
            conn.commit()

    def get_pending_quotes(self) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM quotes WHERE status = 'pending' ORDER BY created_at DESC"
            ).fetchall()
            result = []
            for row in rows:
                d = dict(row)
                d['parts'] = json.loads(d.get('parts_json', '[]'))
                result.append(d)
            return result


# ============================================================================
# Pricing Engine — calls advanced.parts API (signal-only, last-sale anchored)
# ============================================================================


PRICING_API_URL = os.getenv("PRICING_API_URL", "https://advanced.parts/api/pricing/quote")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "")
PRICING_API_TIMEOUT = 15


def compute_price(part_number: str, unit_cost: float,
                  sale_history: List[Dict], customer_id: Optional[int] = None,
                  condition: str = "AR", quantity: int = 1) -> Tuple[float, str]:
    """
    Compute suggested price via advanced.parts pricing API.

    Uses the signal-only pricing engine (last-sale anchored, condition-adjusted,
    quotability-gated). Falls back to basic history lookup if API unreachable.

    Returns: (price, method)
    """
    # Try pricing API first
    if HERMES_API_KEY:
        try:
            api_result = _call_pricing_api(part_number, condition, quantity, customer_id)
            if api_result:
                price = api_result.get("unit_price") or 0
                method = api_result.get("pricing_method", "API")
                requires_rfq = api_result.get("requires_rfq", False)

                if requires_rfq:
                    return 0, "QUOTABILITY_BLOCKED"
                if price and price >= MIN_AUTO_QUOTE_PRICE:
                    return price, method
                if price and price > 0:
                    return 0, "NEEDS_MANUAL"
                # API returned null price — fall through to local fallback
        except Exception as e:
            log.warning("Pricing API failed for %s, using fallback: %s", part_number, e)

    # Fallback: basic last-sale lookup (no cost-plus — costs are unreliable)
    return _compute_price_fallback(part_number, unit_cost, sale_history, customer_id)


def _call_pricing_api(part_number: str, condition: str, quantity: int,
                      customer_id: Optional[int] = None) -> Optional[Dict]:
    """Call advanced.parts pricing API."""
    import requests as _requests

    payload = {
        "items": [{"part_number": part_number, "condition": condition, "quantity": quantity}],
        "tier": "new",  # Default tier for ILS customers
    }
    if customer_id:
        payload["customer_id"] = customer_id

    resp = _requests.post(
        PRICING_API_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {HERMES_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=PRICING_API_TIMEOUT,
    )

    if resp.status_code == 200:
        data = resp.json()
        items = data.get("items", [])
        if items:
            return items[0]
    else:
        log.warning("Pricing API returned %d: %s", resp.status_code, resp.text[:200])

    return None


def _compute_price_fallback(part_number: str, unit_cost: float,
                            sale_history: List[Dict],
                            customer_id: Optional[int] = None) -> Tuple[float, str]:
    """
    Fallback pricing when API is unreachable.

    Uses last-sale anchor (NOT weighted average, NOT cost-plus).
    Cost-plus is meaningless for teardown parts with $0 allocated cost.
    """
    # 1. Customer-specific history — use LAST sale price (not average)
    if customer_id and sale_history:
        customer_lines = [
            l for l in sale_history
            if l.get('order_id') and isinstance(l['order_id'], (list, tuple))
            and len(l['order_id']) > 0
        ]
        if customer_lines:
            last_price = customer_lines[0].get('price_unit', 0)
            if last_price >= MIN_AUTO_QUOTE_PRICE:
                return last_price, "CUSTOMER_HISTORY"
            elif last_price > 0:
                return 0, "NEEDS_MANUAL"

    # 2. General sale history — use LAST sale price, not weighted average
    if sale_history:
        prices = [l['price_unit'] for l in sale_history if l.get('price_unit', 0) > 0]
        if prices:
            last_price = prices[0]  # Most recent sale
            if last_price >= MIN_AUTO_QUOTE_PRICE:
                return last_price, "LAST_SALE"
            return 0, "NEEDS_MANUAL"

    # 3. No sale history = no signal = manual pricing
    # DO NOT fall back to cost markup — costs are $0 or lot-level for teardown parts
    return 0, "NEEDS_MANUAL"


# ============================================================================
# Main Pipeline
# ============================================================================

class AutoQuoteEngine:
    """Orchestrates the ILS RFQ -> V11 draft quote pipeline."""

    def __init__(self):
        self.ils = ILSClient()
        self.v11 = V11Client()
        self.db = QuoteDB()

    def poll_and_process(self) -> List[QuoteResult]:
        """
        Main entry point. Called by cron ticker.

        1. Poll ILS for new RFQs
        2. For each new RFQ:
           a. Check if already seen
           b. Check V11 stock for each part
           c. If any match: find/create customer, draft quote
        3. Return results for Telegram notification
        """
        results = []

        try:
            rfqs = self.ils.get_new_rfqs()
        except Exception as e:
            log.error("ILS poll failed: %s", e)
            return results

        if not rfqs:
            log.debug("No new ILS RFQs")
            return results

        log.info("ILS poll: %d new RFQ(s)", len(rfqs))

        for rfq in rfqs:
            if not rfq.rfq_id or self.db.is_rfq_seen(rfq.rfq_id):
                continue

            self.db.mark_rfq_seen(rfq.rfq_id)
            result = self._process_rfq(rfq)
            results.append(result)

        return results

    def _process_rfq(self, rfq: ILSRfq) -> QuoteResult:
        """Process a single RFQ end-to-end."""
        matched = []
        unmatched = []

        for part in rfq.parts:
            match = self._check_stock(part.part_number)
            if match and match.qty_available > 0:
                matched.append(match)
            else:
                unmatched.append(part.part_number)

        if not matched:
            log.info("RFQ %s: no stock matches (%d parts)", rfq.rfq_id, len(rfq.parts))
            return QuoteResult(
                success=False,
                rfq=rfq,
                unmatched_parts=unmatched,
                error="No parts in stock"
            )

        # Find or create customer
        try:
            partner_id, partner_name, created = self.v11.find_or_create_customer(
                name=rfq.contact_name,
                email=rfq.contact_email,
                company=rfq.company
            )
        except Exception as e:
            log.error("RFQ %s: customer lookup failed: %s", rfq.rfq_id, e)
            return QuoteResult(
                success=False, rfq=rfq,
                matched_parts=matched, unmatched_parts=unmatched,
                error=f"Customer lookup failed: {e}"
            )

        # Create draft quote
        try:
            lines = []
            for m in matched:
                lines.append({
                    'product_id': m.product_id,
                    'quantity': 1,
                    'price_unit': m.suggested_price,
                    'condition': m.condition,
                })

            note_parts = [
                f"ILS RFQ: {rfq.rfq_id}",
                f"Company: {rfq.company}",
                f"Contact: {rfq.contact_name}",
                f"Email: {rfq.contact_email}",
            ]
            if rfq.contact_phone:
                note_parts.append(f"Phone: {rfq.contact_phone}")
            if rfq.contact_title:
                note_parts.append(f"Title: {rfq.contact_title}")
            if rfq.ils_company_id:
                note_parts.append(f"ILS ID: {rfq.ils_company_id}")
            note_parts.append("Auto-generated by Hermes")
            note = "\n".join(note_parts)

            order = self.v11.create_draft_quote(partner_id, lines, note)
            total = sum(m.suggested_price for m in matched)

            # Save to tracking DB
            parts_data = [
                {
                    'part_number': m.part_number,
                    'price': m.suggested_price,
                    'method': m.pricing_method,
                    'condition': m.condition,
                    'qty': m.qty_available,
                }
                for m in matched
            ]
            self.db.save_quote(
                rfq_id=rfq.rfq_id,
                order_id=order['id'],
                order_name=order['name'],
                partner_id=partner_id,
                company=rfq.company,
                contact_email=rfq.contact_email,
                contact_name=rfq.contact_name,
                parts=parts_data,
                total_amount=total,
            )

            # Log to quote memory network for historical pricing recall
            try:
                from services.quote_memory import QuoteMemory
                qm = QuoteMemory(self.db.db_path)
                memory_parts = [
                    {
                        "part_number": m.part_number,
                        "description": m.description,
                        "condition": m.condition,
                        "quantity": int(m.qty_available),
                        "unit_cost": m.unit_cost,
                        "suggested_price": m.suggested_price,
                        "pricing_method": m.pricing_method,
                    }
                    for m in matched
                ]
                qm.log_from_auto_quote(
                    rfq_id=rfq.rfq_id,
                    company=rfq.company,
                    contact_email=rfq.contact_email,
                    parts=memory_parts,
                    order_id=order['id'],
                    order_name=order['name'],
                )
            except Exception as e:
                log.warning("Quote memory log failed (non-blocking): %s", e)

            log.info(
                "RFQ %s -> %s | %d parts matched | $%.2f | customer: %s",
                rfq.rfq_id, order['name'], len(matched), total, partner_name
            )

            return QuoteResult(
                success=True,
                order_id=order['id'],
                order_name=order['name'],
                partner_id=partner_id,
                partner_name=partner_name,
                total_amount=total,
                matched_parts=matched,
                unmatched_parts=unmatched,
                rfq=rfq,
                created_customer=created,
            )

        except Exception as e:
            log.error("RFQ %s: quote creation failed: %s", rfq.rfq_id, e)
            return QuoteResult(
                success=False, rfq=rfq,
                matched_parts=matched, unmatched_parts=unmatched,
                error=f"Quote creation failed: {e}"
            )

    def _check_stock(self, part_number: str) -> Optional[StockMatch]:
        """Check V11 stock for a single part and compute pricing."""
        try:
            product = self.v11.find_part(part_number)
            if not product:
                return None

            product_id = product['id']
            qty = product.get('qty_available', 0)
            tmpl_id_raw = product.get('product_tmpl_id')
            tmpl_id = tmpl_id_raw[0] if isinstance(tmpl_id_raw, (list, tuple)) else tmpl_id_raw

            # Get template info for cost/description
            tmpl = self.v11.get_template_info(tmpl_id) if tmpl_id else {}
            unit_cost = tmpl.get('list_price', 0) or 0
            description = tmpl.get('description_sale', '') or tmpl.get('name', '') or ''

            # Get condition from stock lot (x_studio_field_6Zrwg on stock.production.lot)
            condition = "AR"
            try:
                lots = self.v11.search_read(
                    'stock.production.lot',
                    [['product_id', '=', product_id], ['product_id.qty_available', '>', 0]],
                    ['name', 'x_studio_field_6Zrwg'],
                    limit=1, order='id desc'
                )
                if lots and lots[0].get('x_studio_field_6Zrwg'):
                    condition = lots[0]['x_studio_field_6Zrwg']
            except Exception as e:
                log.debug("Lot condition lookup failed for %s: %s", part_number, e)

            # Get sale history for pricing
            history = self.v11.get_sale_history(part_number)
            price, method = compute_price(part_number, unit_cost, history)

            # Pull enrichment context from pricing API (platform, OEM, IDG flag)
            platform = ""
            oem = ""
            is_idg = part_number.startswith(IDG_PREFIXES)
            confidence = 0

            if HERMES_API_KEY:
                try:
                    api_result = _call_pricing_api(part_number, "AR", 1)
                    if api_result:
                        price = api_result.get("unit_price") or price
                        method = api_result.get("pricing_method") or method
                        confidence = api_result.get("confidence_score") or 0
                        requires_rfq = api_result.get("requires_rfq", False)
                        if requires_rfq:
                            price = 0
                            method = "QUOTABILITY_BLOCKED"
                        # Pull context fields from enriched response
                        platform = api_result.get("platform", "") or ""
                        oem = api_result.get("oem", "") or ""
                        if api_result.get("is_idg"):
                            is_idg = True
                except Exception as e:
                    log.debug("Pricing API enrichment failed for %s: %s", part_number, e)

            return StockMatch(
                part_number=part_number,
                product_id=product_id,
                template_id=tmpl_id or 0,
                qty_available=qty,
                condition=condition,
                unit_cost=unit_cost,
                suggested_price=price,
                description=description,
                pricing_method=method,
                platform=platform,
                oem=oem,
                is_idg=is_idg,
                confidence=confidence,
            )
        except Exception as e:
            log.error("Stock check failed for %s: %s", part_number, e)
            return None

    def approve_quote(self, quote_id: int) -> Optional[Dict]:
        """Mark a quote as approved. Returns quote data for email drafting."""
        quote = self.db.get_quote(quote_id)
        if not quote:
            return None
        self.db.update_status(quote_id, 'approved')
        try:
            from services.quote_memory import QuoteMemory
            QuoteMemory(self.db.db_path).update_response(quote_id, "approved")
        except Exception:
            pass
        return quote

    def reject_quote(self, quote_id: int) -> bool:
        """Reject a quote and cancel the V11 draft SO."""
        quote = self.db.get_quote(quote_id)
        if not quote:
            return False
        self.db.update_status(quote_id, 'rejected')
        try:
            from services.quote_memory import QuoteMemory
            QuoteMemory(self.db.db_path).update_response(quote_id, "rejected")
        except Exception:
            pass
        if quote.get('order_id'):
            self.v11.cancel_draft_quote(quote['order_id'])
        return True
