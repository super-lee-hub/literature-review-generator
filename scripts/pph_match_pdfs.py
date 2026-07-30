"""Match eligible papers to PDFs across all known locations."""
import csv, os, json, re, shutil
from collections import defaultdict

AUDIT_PATH = r"C:\Users\12130\Desktop\新建文件夹\博good good study\促销与使用意愿\literature_rebuild_20260727\21_journal_index_eligibility_audit.csv"
MEMBERSHIP_PATH = r"C:\Users\12130\Desktop\新建文件夹\博good good study\促销与使用意愿\literature_rebuild_20260727\section_memberships.csv"
WORK_DIR = r"D:\auto-generate\output\pph_review_work"
STAGE1_PDF_DIR = os.path.join(WORK_DIR, "stage1_pdfs")

def normalize_title(t):
    t = re.sub(r"[^\w\s]", "", t.lower())
    t = re.sub(r"\s+", " ", t).strip()
    return t[:100]

def get_keywords(title, min_len=4, max_words=4):
    words = [w for w in normalize_title(title).split() if len(w) > min_len]
    return words[:max_words]

# Load eligible papers
with open(AUDIT_PATH, "r", encoding="utf-8-sig") as f:
    audit = list(csv.DictReader(f))

eligible = []
for r in audit:
    if r["eligible"] == "YES":
        eligible.append({
            "key": r["zotero_key"],
            "title": r["title"],
            "journal": r["journal"],
            "language": r["language"],
        })

print(f"Eligible papers: {len(eligible)}")

# Load section memberships for collection mapping
with open(MEMBERSHIP_PATH, "r", encoding="utf-8-sig") as f:
    member_rows = list(csv.DictReader(f))

key_to_collections = defaultdict(set)
for row in member_rows:
    zk = row.get("resolved_zotero_key", "")
    if zk:
        key_to_collections[zk].add(row.get("collection_name", ""))

# Find PDFs
search_roots = [
    r"C:\Users\12130\Desktop\新建文件夹\博good good study\促销与使用意愿",
    r"D:\zotero library\平台既往让利与价格不公平感_新版文献包_20260727",
]

all_pdfs = []
for base in search_roots:
    if not os.path.isdir(base):
        continue
    for root, dirs, files in os.walk(base):
        depth = root.replace(base, "").count(os.sep)
        if depth > 4:
            dirs.clear()
            continue
        for fname in files:
            if fname.lower().endswith(".pdf"):
                all_pdfs.append((os.path.join(root, fname), fname))

print(f"PDFs found: {len(all_pdfs)}")

# Match
matched = 0
unmatched_list = []
for paper in eligible:
    keywords = get_keywords(paper["title"])
    if not keywords:
        unmatched_list.append(paper)
        continue
    found = False
    for pdf_path, pdf_name in all_pdfs:
        pdf_lower = pdf_name.lower()
        if all(kw in pdf_lower for kw in keywords):
            paper["pdf_path"] = pdf_path
            paper["collections"] = sorted(key_to_collections.get(paper["key"], set()))
            matched += 1
            found = True
            break
    if not found:
        unmatched_list.append(paper)

print(f"Matched: {matched}/{len(eligible)}")
print(f"Unmatched: {len(unmatched_list)}")

# Show unmatched
for p in unmatched_list[:15]:
    print(f"  {p['key']}: {p['title'][:80]}")

# Save matched manifest
os.makedirs(WORK_DIR, exist_ok=True)
manifest_path = os.path.join(WORK_DIR, "pdf_match_manifest.json")
matched_papers = [p for p in eligible if "pdf_path" in p]
with open(manifest_path, "w", encoding="utf-8") as f:
    json.dump(matched_papers, f, ensure_ascii=False, indent=2)
print(f"\nMatched manifest: {manifest_path}")

# Also save a CSV
csv_path = os.path.join(WORK_DIR, "stage1_papers.csv")
with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=["key", "title", "journal", "language", "collections", "pdf_path"])
    writer.writeheader()
    for p in matched_papers:
        writer.writerow({
            "key": p["key"],
            "title": p["title"],
            "journal": p["journal"],
            "language": p["language"],
            "collections": ";".join(p.get("collections", [])),
            "pdf_path": p.get("pdf_path", ""),
        })
print(f"Stage 1 CSV: {csv_path}")

# Create consolidated PDF directory
os.makedirs(STAGE1_PDF_DIR, exist_ok=True)
copied = 0
for p in matched_papers:
    src = p.get("pdf_path", "")
    if src and os.path.exists(src):
        dst_name = f"{p['key']}__{os.path.basename(src)}"
        dst = os.path.join(STAGE1_PDF_DIR, dst_name)
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
            copied += 1

print(f"PDFs copied to stage1_pdfs: {copied}")
print(f"Stage 1 PDF directory: {STAGE1_PDF_DIR}")
