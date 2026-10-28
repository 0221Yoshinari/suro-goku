"""デイリーレポートを1通送る。--if-due のときは 23:50〜翌朝6:00 だけ。"""

from dotenv import load_dotenv

load_dotenv()

from src.marketing.daily_report import main

if __name__ == "__main__":
    raise SystemExit(main())
