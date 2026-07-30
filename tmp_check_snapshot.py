import json
import os

snapshot_path = os.path.join(
    os.environ["USERPROFILE"],
    "Desktop", "新建文件夹", "博good good study",
    "促销与使用意愿", "literature_rebuild_20260727",
    "19_final_live_zotero_snapshot.json"
)
with open(snapshot_path, "r", encoding="utf-8") as f:
    data = json.load(f)

print("=== Managed Collections ===")
for c in data["managed_collections"]:
    print(f"  {c['name']} (key={c['key']}, parent={c.get('parent')})")

print("\n=== Items by Collection ===")
for k, v in data["items_by_collection"].items():
    if isinstance(v, list):
        print(f"  {k}: {len(v)} items")
    else:
        print(f"  {k}: {v}")

print(f"\nTotal items in managed: {data['items_in_managed_collections']}")
print(f"Unique paper keys: {data['unique_paper_keys_in_managed']}")
