"""Preflight check for PPH review project."""
import json, csv, os, sys, subprocess
from collections import Counter, defaultdict
from datetime import datetime

WORK_DIR = r"D:\auto-generate\output\pph_review_work"
CONFIG_PATH = r"D:\auto-generate\config.ini"
ZOTERO_LIBRARY = r"D:\zotero library\Zotero\storage"
MEMBERSHIPS = r"C:\Users\12130\Desktop\新建文件夹\博good good study\促销与使用意愿\literature_rebuild_20260727\section_memberships.csv"

report_lines = []
def log(msg):
    print(msg)
    report_lines.append(msg)

log("# Preflight Report — PPH Review")
log(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
log("")
log("## 1. Python Environment")
log(f"Python: {sys.version}")
log("")

log("## 2. main.py --help")
result = subprocess.run([sys.executable, "main.py", "--help"], capture_output=True, text=True, cwd=r"D:\auto-generate")
log(f"Exit code: {result.returncode}")
for p in ["--zotero-report", "--library-path", "--analyze-only", "--generate-outline",
          "--generate-review", "--validate-review", "--summary-file", "--summary-source",
          "--reuse-summary-file", "--retry-failed", "--generate-section", "--retry-review-failed"]:
    for line in result.stdout.split("\n"):
        if p in line:
            log(f"  {line.strip()}")
            break
log("")

log("## 3. Config File")
log(f"Exists: {os.path.exists(CONFIG_PATH)}")
import configparser
cfg = configparser.ConfigParser()
cfg.read(CONFIG_PATH, encoding='utf-8')
zr = cfg.get('Paths', 'zotero_report', fallback='')
lp = cfg.get('Paths', 'library_path', fallback='')
log(f"zotero_report config: {zr}")
log(f"zotero_report exists: {os.path.exists(zr)}")
log(f"library_path config: {lp}")
log(f"library_path exists: {os.path.exists(lp)}")
log("")

log("## 4. Zotero Library/Storage")
if os.path.isdir(ZOTERO_LIBRARY):
    items = os.listdir(ZOTERO_LIBRARY)
    log(f"Storage directories: {len(items)}")
else:
    log(f"NOT FOUND")
log("")

log("## 5. Collection Membership (from section_memberships.csv)")
with open(MEMBERSHIPS, "r", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    member_rows = list(reader)

unique_papers = set()
coll_papers = defaultdict(set)
for row in member_rows:
    unique_papers.add(row["paper_id"])
    coll_papers[row["collection_name"]].add(row["paper_id"])

collections_order = [
    "00_种子文献与总览", "01_综述_动态定价与价格劣势", "02_综述_平台既往让利与补贴",
    "03_假设_既往让利到价格不公平感", "04_假设_价格不公平感到持续使用",
    "05_假设_商业模式主观知识调节", "90_范围_亲历与知晓", "91_范围_适用边界与伦理"
]
log(f"Total membership rows: {len(member_rows)}")
log(f"Unique paper IDs: {len(unique_papers)}")
for cn in collections_order:
    papers = coll_papers.get(cn, set())
    log(f"  {cn}: {len(papers)} unique papers")
log("")

log("## 6. Journal Eligibility (per paper, deduplicated)")
paper_roles = {}
for row in member_rows:
    pid = row["paper_id"]
    if pid not in paper_roles:
        paper_roles[pid] = set()
    for r in row.get("evidence_roles", "").split(";"):
        r = r.strip()
        if r:
            paper_roles[pid].add(r)

eligible = 0
non_eligible = 0
uncertain = 0
for pid, roles in paper_roles.items():
    is_ssci = "SSCI" in roles
    is_cssci = "CSSCI" in roles
    is_cssci_ext = "CSSCI扩展版" in roles
    is_non_formal = "non-formal" in roles
    is_do_not_cite = "do-not-cite" in roles
    if is_non_formal or is_do_not_cite:
        non_eligible += 1
    elif is_ssci or is_cssci or is_cssci_ext:
        eligible += 1
    else:
        uncertain += 1

log(f"Eligible (SSCI/CSSCI/CSSCI-ext): {eligible}")
log(f"Non-eligible (non-formal/do-not-cite): {non_eligible}")
log(f"Uncertain (no clear journal status): {uncertain}")
log("")

log("## 7. Known Exclusions")
log("  Schuhmacher et al. 2025: Journal of Service Management Research — not SSCI")
log("  Li & Zhang 2025 (SSRN): working paper — not formally published")
log("")

log("## 8. PDF Status (sample)")
known_keys = set()
for row in member_rows:
    zk = row.get("resolved_zotero_key", "").strip()
    if zk:
        known_keys.add(zk)

pdf_found = 0
pdf_missing = 0
sample = list(known_keys)[:50]
for zk in sample:
    storage_dir = os.path.join(ZOTERO_LIBRARY, zk)
    if os.path.isdir(storage_dir):
        pdfs = [f for f in os.listdir(storage_dir) if f.lower().endswith('.pdf')]
        if pdfs:
            pdf_found += 1
        else:
            pdf_missing += 1
    else:
        pdf_missing += 1

log(f"Sample ({len(sample)} keys): {pdf_found} PDFs found, {pdf_missing} missing")
log(f"Total unique Zotero keys: {len(known_keys)}")
log("")

log("## 9. Readiness")
issues = []
if not os.path.exists(ZOTERO_LIBRARY):
    issues.append("Zotero library path not found")
if eligible == 0:
    issues.append("No eligible papers")
if issues:
    for i in issues:
        log(f"  BLOCKER: {i}")
    log("NOT READY")
else:
    log("READY for formal corpus screening")
    log(f"Estimated eligible unique papers: ~{eligible}")

os.makedirs(WORK_DIR, exist_ok=True)
report_path = os.path.join(WORK_DIR, "00_preflight_report.md")
with open(report_path, "w", encoding="utf-8") as f:
    f.write("\n".join(report_lines))
print(f"\nReport: {report_path}")
