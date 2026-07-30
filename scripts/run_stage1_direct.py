"""Run stage 1 directly in PDF folder mode, bypassing runtime source_intake."""
import sys, os, logging
sys.path.insert(0, r"D:\auto-generate")

from main import LiteratureReviewGenerator
from config_loader import load_config

PDF_FOLDER = r"D:\auto-generate\output\pph_review_work\stage1_pdfs"
PROJECT_NAME = "pph_master_stage1"
REUSE_SUMMARIES = [
    r"D:\auto-generate\output\促销综述第一节全__20260523_124244\artifacts\促销综述第一节全_summaries.json",
]

def main():
    # Create generator with direct mode
    gen = LiteratureReviewGenerator(
        config_file="config.ini",
        project_name=PROJECT_NAME,
        pdf_folder=PDF_FOLDER,
        zotero_report=None,
        library_path=None,
    )
    
    # Force direct mode (overriding any config-based defaults)
    gen.mode = "direct"
    gen.zotero_report = None
    
    # Set up reuse
    for sf in REUSE_SUMMARIES:
        if os.path.exists(sf) and sf not in gen.reuse_summary_files:
            gen.reuse_summary_files.append(sf)
    gen.reuse_stage1 = True
    
    # Run stage 1
    print(f"Running stage 1 in {gen.mode} mode...")
    print(f"PDF folder: {gen.pdf_folder}")
    print(f"Reuse files: {gen.reuse_summary_files}")
    
    success = gen.run_stage_one()
    print(f"Stage 1 result: {'SUCCESS' if success else 'FAILED'}")
    
    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main())
