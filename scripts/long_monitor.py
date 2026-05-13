
import sys
import os
import json
import time
from datetime import datetime

# Add project root to sys.path
sys.path.append(os.getcwd())

from mcp_tools.monitor import monitor

def run_long_monitor(duration_hours=1, interval_seconds=300):
    start_time = time.time()
    end_time = start_time + (duration_hours * 3600)
    
    report_path = "logs/monitor_report.json"
    print(f"Starting long monitor for {duration_hours} hour(s)...")
    print(f"Results will be written to {report_path}")
    
    reports = []
    
    while time.time() < end_time:
        print(f"[{datetime.now().isoformat()}] Polling monitor for 5 minutes...")
        # We poll in 5-minute chunks to keep the process alive and responsive
        chunk_report = monitor(duration_seconds=interval_seconds, poll_interval_seconds=30)
        reports.append(chunk_report)
        
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(reports, f, indent=2)
            
        # Small sleep to prevent tight looping if duration is small
        time.sleep(1)

    print(f"Long monitor finished. Total reports: {len(reports)}")

if __name__ == "__main__":
    run_long_monitor(duration_hours=1, interval_seconds=300)
