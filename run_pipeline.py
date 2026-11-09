"""CLI: python run_pipeline.py [--only rfq_01_email]"""
import argparse

from quote_desk.pipeline import run

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="RFQ file stem to process")
    run(ap.parse_args().only)
