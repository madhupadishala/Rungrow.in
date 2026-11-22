"""Real file artifact generation and robots-compliant public-page extraction."""
from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse

from .commercial_intelligence import RobotsCompliantHttpFetcher, RobotsDisallowed
from .skill_registry import PRODUCT_PLAYBOOKS

ARTIFACT_ROOT = Path(os.getenv("RUNGROW_ARTIFACT_ROOT", "/tmp/rungrow-artifacts")).resolve()
PALETTE = {"marigold": "#E7A928", "haldi": "#F4C95D", "terracotta": "#B85C38",
           "ink": "#3E342B", "cream": "#FFF8E8", "leaf": "#647A50"}

class ArtifactPolicyError(PermissionError):
    pass

class ArtifactEngine:
    def __init__(self, fetcher=None, artifact_root: Path | None = None):
        self.fetcher = fetcher or RobotsCompliantHttpFetcher(
            allow_domains={x.strip().lower() for x in os.getenv("RUNGROW_ARTIFACT_ALLOW_DOMAINS", "").split(",") if x.strip()}
        )
        self.root = (artifact_root or ARTIFACT_ROOT).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _write_path(self, suffix: str) -> Path:
        folder = self.root / date.today().isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{uuid.uuid4().hex}.{suffix}"

    @staticmethod
    def _safe_text(value, limit=4000):
        return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or ""))[:limit]

    async def headless_browser_page_extract(self, url: str) -> dict:
        # Robots/domain/SSRF validation is performed before a browser is allowed to load.
        fetched = await self.fetcher.fetch_page(url)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ArtifactPolicyError("Only public HTTP(S) pages can be extracted")
        html = fetched.get("html", "")
        if not html:
            html = fetched.get("text", "")
        rendered = False
        headings, contacts, services, body_text = [], [], [], ""
        if os.getenv("RUNGROW_ENABLE_PLAYWRIGHT", "").lower() == "true":
            try:
                from playwright.async_api import async_playwright
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    page = await browser.new_page(java_script_enabled=True)
                    origin = f"{parsed.scheme}://{parsed.netloc}"
                    async def guard(route):
                        request_url = route.request.url
                        try:
                            normalized, host = self.fetcher._validate_url(request_url)
                            robots = await self.fetcher._get_robots(host, urlparse(normalized).scheme)
                            allowed = robots.can_fetch(self.fetcher.user_agent, normalized)
                        except Exception:
                            allowed = False
                        if not allowed:
                            await route.abort()
                        else:
                            await route.continue_()
                    await page.route("**/*", guard)
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    if response is None or response.status >= 400:
                        raise ArtifactPolicyError("Public page returned an unsuccessful response")
                    body_text = await page.locator("body").inner_text(timeout=8000)
                    headings = await page.locator("h1,h2,h3").all_text_contents()
                    contacts = await page.locator("a[href^='mailto:'],a[href^='tel:']").evaluate_all(
                        "(els) => els.map(e => ({text:e.innerText,href:e.href}))"
                    )
                    services = await page.locator("[class*='service'],[id*='service'],article").all_text_contents()
                    await browser.close()
                    rendered = True
            except ImportError:
                pass
            except Exception as exc:
                if isinstance(exc, ArtifactPolicyError):
                    raise
                # Browser failures are not silently converted to a claim of JS extraction.
                body_text = ""
        if not body_text:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for node in soup(["script", "style", "noscript", "svg"]):
                node.decompose()
            headings = [self._safe_text(x.get_text(" ", strip=True), 240) for x in soup.select("h1,h2,h3")][:40]
            contacts = [{"text": self._safe_text(a.get_text(" ", strip=True), 120),
                         "href": self._safe_text(a.get("href", ""), 300)}
                        for a in soup.select("a[href^='mailto:'],a[href^='tel:']")][:40]
            body_text = self._safe_text(soup.get_text(" ", strip=True), 16000)
            services = [self._safe_text(x.get_text(" ", strip=True), 300)
                        for x in soup.select("[class*='service'],[id*='service'],article")][:40]
        return {"url": url, "title": self._safe_text(fetched.get("title", "")),
                "headings": headings[:40], "contact_metadata": contacts[:40],
                "service_offerings": services[:40], "body_text": body_text[:16000],
                "robots_allowed": True, "domain_allowlisted": True,
                "javascript_rendered": rendered, "source_timestamp": fetched.get("fetched_at")}

    def generate_marketing_image(self, *, product_id: str, headline: str,
                                 subheadline: str = "", format: str = "square",
                                 highlights: list[str] | None = None) -> dict:
        playbook = PRODUCT_PLAYBOOKS.get(product_id)
        if not playbook or playbook.get("stage") == "DISCOVERY_ONLY":
            raise ArtifactPolicyError("Product is not approved for marketing collateral")
        if format not in {"square", "story"}:
            raise ValueError("format must be square or story")
        from PIL import Image, ImageDraw, ImageFont
        width, height = (1080, 1080) if format == "square" else (1080, 1920)
        image = Image.new("RGB", (width, height), PALETTE["cream"])
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, width, int(height * .035)), fill=PALETTE["marigold"])
        draw.ellipse((width-370, 90, width+120, 580), fill=PALETTE["haldi"])
        draw.rounded_rectangle((64, 70, width-64, height-70), radius=34,
                               outline=PALETTE["terracotta"], width=5)
        font_bold = ImageFont.load_default(size=64 if format == "square" else 76)
        font_regular = ImageFont.load_default(size=34)
        font_small = ImageFont.load_default(size=28)
        draw.text((110, 150), "RUNGROW  •  BUSINESS GROWTH", font=font_small, fill=PALETTE["terracotta"])
        y = 300
        for line in self._wrap(headline, 24 if format == "square" else 20):
            draw.text((110, y), line, font=font_bold, fill=PALETTE["ink"])
            y += 86
        y += 40
        for line in self._wrap(subheadline, 42):
            draw.text((110, y), line, font=font_regular, fill=PALETTE["ink"])
            y += 48
        y += 55
        for item in (highlights or [])[:5]:
            draw.rounded_rectangle((110, y, width-110, y+76), radius=18, fill="#FFFFFF", outline=PALETTE["haldi"], width=3)
            draw.text((140, y+19), "✓  " + self._safe_text(item, 64), font=font_small, fill=PALETTE["leaf"])
            y += 100
        footer_y = height - 210
        draw.rectangle((100, footer_y, width-100, footer_y+3), fill=PALETTE["terracotta"])
        draw.text((110, footer_y+28), "Practical systems for ambitious Indian SMBs", font=font_small, fill=PALETTE["ink"])
        draw.text((110, footer_y+80), "AI-assisted • Human-governed • RunGrow", font=font_small, fill=PALETTE["terracotta"])
        path = self._write_path("png")
        image.save(path, format="PNG", optimize=True)
        data = path.read_bytes()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("PNG artifact verification failed")
        return {"artifact_id": path.stem, "path": str(path), "filename": path.name,
                "mime_type": "image/png", "bytes": len(data), "width": width, "height": height,
                "sha256": __import__("hashlib").sha256(data).hexdigest()}

    @staticmethod
    def _wrap(text, max_chars):
        words = str(text).split()
        lines, current = [], ""
        for word in words:
            candidate = (current + " " + word).strip()
            if len(candidate) > max_chars and current:
                lines.append(current); current = word
            else:
                current = candidate
        if current: lines.append(current)
        return lines or [""]

    def generate_pdf_brochure_and_quote(self, *, product_id: str, customer_name: str,
                                         tier: str | None = None, months: int = 12,
                                         quote: bool = False, items: list[dict] | None = None,
                                         gst_rate: float = 18.0) -> dict:
        playbook = PRODUCT_PLAYBOOKS.get(product_id)
        if not playbook or playbook.get("stage") == "DISCOVERY_ONLY":
            raise ArtifactPolicyError("Product is not approved for commercial collateral")
        if months < 1 or months > 36 or not 0 <= gst_rate <= 28:
            raise ValueError("Invalid quote duration or GST rate")
        line_items = items or []
        if quote:
            if playbook.get("pricing_status") != "PROVISIONAL_FOUNDER_APPROVED":
                raise ArtifactPolicyError("No Founder-approved price book exists for this product")
            tiers = playbook.get("tiers", {})
            if tier not in tiers:
                raise ArtifactPolicyError("Requested tier is not in the approved price book")
            monthly = int(tiers[tier]["monthly_inr"])
            line_items = [{"description": f"{tier} subscription × {months} months",
                           "quantity": 1, "unit_price_inr": monthly * months}]
        if quote and not line_items:
            raise ValueError("Quote requires itemized line items")
        for item in line_items:
            if float(item.get("quantity", 0)) <= 0 or float(item.get("unit_price_inr", -1)) < 0:
                raise ValueError("Quote line items require positive quantity and non-negative INR price")
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
        path = self._write_path("pdf")
        doc = SimpleDocTemplate(str(path), pagesize=A4, rightMargin=18*mm, leftMargin=18*mm,
                                topMargin=18*mm, bottomMargin=18*mm)
        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(name="RGTitle", parent=styles["Title"], textColor=colors.HexColor(PALETTE["terracotta"])))
        story = [Paragraph("RunGrow", styles["Heading2"]),
                 Paragraph("Commercial Proposal" if quote else "Product Brochure", styles["RGTitle"]),
                 Paragraph(self._safe_text(product_id), styles["Heading2"]), Spacer(1, 8*mm)]
        if quote:
            story += [Paragraph("Prepared for: " + self._safe_text(customer_name), styles["BodyText"]),
                      Paragraph("Currency: INR • GST shown separately", styles["BodyText"]), Spacer(1, 5*mm)]
            rows = [["Description", "Qty", "Unit price (INR)", "Amount (INR)"]]
            subtotal = 0.0
            for item in line_items:
                amount = float(item["quantity"]) * float(item["unit_price_inr"])
                subtotal += amount
                rows.append([self._safe_text(item["description"]), str(item["quantity"]),
                             f"{float(item['unit_price_inr']):,.2f}", f"{amount:,.2f}"])
            gst = round(subtotal * gst_rate / 100, 2)
            rows += [["", "", "Subtotal", f"{subtotal:,.2f}"],
                     ["", "", f"GST ({gst_rate:g}%)", f"{gst:,.2f}"],
                     ["", "", "Total incl. GST", f"{subtotal+gst:,.2f}"]]
            table = Table(rows, colWidths=[78*mm, 18*mm, 38*mm, 38*mm], repeatRows=1)
            table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor(PALETTE["marigold"])),
                ("GRID",(0,0),(-1,-1),.4,colors.HexColor("#D7C7A8")),("ALIGN",(1,1),(-1,-1),"RIGHT"),
                ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("BOTTOMPADDING",(0,0),(-1,-1),8),
                ("TOPPADDING",(0,0),(-1,-1),8)]))
            story += [table, Spacer(1, 8*mm),
                      Paragraph("Commercial terms are subject to final agreement and applicable tax review.", styles["BodyText"])]
        else:
            story += [Paragraph(self._safe_text(playbook.get("icp", "Business operations")), styles["BodyText"]),
                      Spacer(1, 4*mm), Paragraph("Practical workflows • Measurable outcomes • Human-governed execution", styles["BodyText"]),
                      PageBreak(), Paragraph("Product highlights", styles["Heading1"])]
            for line in ("Structured lead and customer workflows", "Operational visibility and reporting",
                         "Role-based execution with human approvals", "Evidence-backed customer onboarding"):
                story.append(Paragraph("• " + line, styles["BodyText"])); story.append(Spacer(1, 3*mm))
        doc.build(story)
        data = path.read_bytes()
        if not data.startswith(b"%PDF-"):
            raise RuntimeError("PDF artifact verification failed")
        return {"artifact_id": path.stem, "path": str(path), "filename": path.name,
                "mime_type": "application/pdf", "bytes": len(data),
                "sha256": __import__("hashlib").sha256(data).hexdigest()}

    def manage_excel_tracker(self, *, tracker_type: str, rows: list[dict] | None = None) -> dict:
        from openpyxl import Workbook, load_workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
        if tracker_type not in {"lead_pipeline", "social_calendar", "customer_onboarding"}:
            raise ValueError("Unsupported tracker_type")
        wb=Workbook()
        summary=wb.active; summary.title="Summary"
        sheet_name={"lead_pipeline":"Leads","social_calendar":"30-Day Calendar",
                    "customer_onboarding":"Onboarding"}[tracker_type]
        ws=wb.create_sheet(sheet_name)
        if tracker_type=="lead_pipeline":
            headers=["Lead","Company","Product","Stage","Owner","Value INR","Next Follow-up","Probability","Weighted INR"]
            data=rows or []
            ws.append(headers)
            for item in data:
                row=ws.max_row+1
                ws.append([item.get("lead",""),item.get("company",""),item.get("product",""),
                    item.get("stage","New"),item.get("owner",""),float(item.get("value_inr",0)),
                    item.get("next_follow_up",""),float(item.get("probability",0)),f"=F{row}*H{row}"])
            if not data: ws.append(["","","","New","",0,"",0,"=F2*H2"])
            summary_rows=[["Metric","Value"],["Total Leads","=COUNTA(Leads!A:A)-1"],
                ["Pipeline INR","=SUM(Leads!F:F)"],["Weighted Pipeline INR","=SUM(Leads!I:I)"]]
        elif tracker_type=="social_calendar":
            headers=["Date","Channel","Content Pillar","Topic","Format","CTA","UTM Campaign","Status","Approval"]
            data=rows or [{"date":(date.today()+timedelta(days=i)).isoformat(),
                "channel":["LinkedIn","Instagram","X","YouTube Shorts"][i%4],
                "pillar":"Product offer" if i%5==4 else "Problem-solving",
                "topic":["Lead follow-up","Customer trust","Operations","Revenue visibility","Product walkthrough"][i%5],
                "format":["Carousel","Short video","Text post"][i%3],"cta":"Request a guided demo",
                "utm_campaign":f"rungrow-day-{i+1}","status":"DRAFT","approval":"REQUIRED"} for i in range(30)]
            ws.append(headers)
            for x in data: ws.append([x.get("date",""),x.get("channel",""),x.get("pillar",""),x.get("topic",""),
                x.get("format",""),x.get("cta",""),x.get("utm_campaign",""),x.get("status","DRAFT"),x.get("approval","REQUIRED")])
            summary_rows=[["Metric","Value"],["Scheduled Posts","=COUNTA('30-Day Calendar'!A:A)-1"],
                ["Approval Required",'=COUNTIF(\'30-Day Calendar\'!I:I,"REQUIRED")']]
        else:
            headers=["Customer","Product","Owner","Kickoff","Milestone","Due Date","Status","Completion %","Evidence Link"]
            data=rows or []
            ws.append(headers)
            for x in data: ws.append([x.get("customer",""),x.get("product",""),x.get("owner",""),
                x.get("kickoff",""),x.get("milestone",""),x.get("due_date",""),x.get("status","Not Started"),
                float(x.get("completion",0)),x.get("evidence_link","")])
            if not data: ws.append(["","","","","","","Not Started",0,""])
            summary_rows=[["Metric","Value"],["Milestones","=COUNTA(Onboarding!E:E)-1"],
                ["Completed",'=COUNTIF(Onboarding!G:G,"Complete")'],
                ["Average Completion","=IFERROR(AVERAGE(Onboarding!H:H),0)"]]
        for cell in ws[1]:
            cell.font=Font(bold=True,color="FFFFFF")
            cell.fill=PatternFill("solid",fgColor="B85C38")
            cell.alignment=Alignment(horizontal="center",wrap_text=True)
        ws.freeze_panes="A2"; ws.auto_filter.ref=ws.dimensions
        for col,header in enumerate(headers,start=1):
            ws.column_dimensions[get_column_letter(col)].width=min(36,max(14,len(header)+4))
        for row in summary_rows: summary.append(row)
        for cell in summary[1]:
            cell.font=Font(bold=True,color="FFFFFF")
            cell.fill=PatternFill("solid",fgColor="E7A928")
        summary.column_dimensions["A"].width=28; summary.column_dimensions["B"].width=30
        path=self._write_path("xlsx"); wb.save(path)
        check=load_workbook(path,read_only=False,data_only=False)
        if check.sheetnames != ["Summary",sheet_name] or check[sheet_name].max_row < 2:
            raise RuntimeError("XLSX artifact verification failed")
        formulas=sum(1 for sheet in check for row in sheet.iter_rows() for cell in row
                     if isinstance(cell.value,str) and cell.value.startswith("="))
        data=path.read_bytes()
        return {"artifact_id":path.stem,"path":str(path),"filename":path.name,
            "mime_type":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "bytes":len(data),"sheets":check.sheetnames,"formula_count":formulas,
            "sha256":__import__("hashlib").sha256(data).hexdigest()}

    def governed_email_payload(self, *, to: list[str], subject: str, body: str,
                               artifacts: list[dict]) -> dict:
        import re
        if not to or len(to)>20 or any(not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", x) for x in to):
            raise ValueError("Valid recipient email addresses are required")
        if not subject.strip() or len(subject)>180 or not body.strip() or len(body)>12000:
            raise ValueError("Subject and body are required and must be within limits")
        if len(artifacts)>10:
            raise ValueError("At most 10 artifact attachments are allowed")
        verified=[]
        for item in artifacts:
            path=Path(str(item.get("path",""))).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                raise ArtifactPolicyError("Attachment is not a verified RunGrow artifact")
            verified.append({"path":str(path),"filename":path.name,
                             "mime_type":item.get("mime_type"),"sha256":item.get("sha256")})
        return {"to":to,"subject":subject,"body":body,"attachments":verified,
                "approval_required":True,"status":"QUEUED_FOR_APPROVAL",
                "dispatch_status":"NOT_DISPATCHED"}
