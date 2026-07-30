import csv, json, os
from collections import defaultdict

MEMBERSHIP = r"C:\Users\12130\Desktop\新建文件夹\博good good study\促销与使用意愿\literature_rebuild_20260727\section_memberships.csv"
AUDIT = r"C:\Users\12130\Desktop\新建文件夹\博good good study\促销与使用意愿\literature_rebuild_20260727\21_journal_index_eligibility_audit.csv"

with open(MEMBERSHIP, "r", encoding="utf-8-sig") as f:
    rows = list(csv.DictReader(f))

seed = {}
for row in rows:
    cn = row.get("collection_name", "")
    pid = row["paper_id"]
    if cn == "00_种子文献与总览":
        if pid not in seed:
            seed[pid] = {"paper_id": pid, "title": row["title"], "zotero_key": row.get("resolved_zotero_key", ""), "in_collections": set()}
    if pid in seed:
        seed[pid]["in_collections"].add(cn)

formal = {"01_综述_动态定价与价格劣势", "02_综述_平台既往让利与补贴", "03_假设_既往让利到价格不公平感", "04_假设_价格不公平感到持续使用", "05_假设_商业模式主观知识调节"}

with open(AUDIT, "r", encoding="utf-8-sig") as f:
    audit_rows = list(csv.DictReader(f))
key_eligible = {r["zotero_key"]: r["eligible"] for r in audit_rows}

covered = []
uncovered = []
for pid, p in seed.items():
    f = p["in_collections"] & formal
    if f:
        covered.append((pid, p, f))
    else:
        uncovered.append((pid, p))

lines = []
lines.append("# Seed Coverage Audit (Collection 00)")
lines.append("")
lines.append("Total seed papers: {}".format(len(seed)))
lines.append("Covered by formal topics: {}".format(len(covered)))
lines.append("Not in any formal topic: {}".format(len(uncovered)))
lines.append("")

if uncovered:
    lines.append("## Uncovered Seed Papers")
    for pid, p in uncovered:
        ok = key_eligible.get(p["zotero_key"], "?")
        others = sorted(p["in_collections"] - {"00_种子文献与总览"})
        lines.append("- {}: {} (eligible={}, in={})".format(pid, p["title"][:80], ok, others))
else:
    lines.append("All seed papers covered.")

out = os.path.join(r"D:\auto-generate\output\pph_review_work", "01_seed_coverage_audit.md")
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("Seed audit: {}".format(out))
print("Total: {}, Covered: {}, Uncovered: {}".format(len(seed), len(covered), len(uncovered)))
