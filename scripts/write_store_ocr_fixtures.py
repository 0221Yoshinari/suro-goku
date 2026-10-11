from pathlib import Path
import json
from datetime import datetime, timezone

ROOT = Path("/Users/kobakeinari/Projects/tamaya-slot-scraper/data/ocr_results")

def rec(name, source, **fields):
    return {
        "source_file": source,
        "source_path": str(ROOT / source),
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "fields": {
            "machine_name": name,
            "bb_count": fields.get("bb"),
            "rb_count": fields.get("rb"),
            "bb_probability": fields.get("bbp"),
            "rb_probability": fields.get("rbp"),
            "synthetic_probability": fields["syn"],
            "total_games": fields["games"],
            "first_hit_count": fields.get("hits"),
            "extra_numbers": [],
        },
        "raw_texts": [],
    }

fixtures = [
    rec("マイジャグラーV", "マイジャグラーV　数値.json", bb=52, rb=53, syn=110.2, games=11571, hits=105, bbp=222.5, rbp=218.3),
    rec("ゴーゴージャグラー3", "ゴーゴージャグラー3　数値.json", bb=40, rb=38, syn=145.0, games=11310, hits=78, bbp=282.8, rbp=297.6),
    rec("ハッピージャグラーV3", "ハッピージャグラーV3　数値.json", bb=18, rb=16, syn=118.5, games=4029, hits=34, bbp=223.8, rbp=251.8),
    rec("プリズムナナ", "スマホナ　数値.json", bb=None, rb=None, syn=270.4, games=9734, hits=36),
    rec("ヴァルヴレイヴ2", "ヴァルヴレイヴ　数値.json", bb=None, rb=None, syn=448.0, games=13440, hits=30),
    rec("スマスロ北斗の拳", "北斗の拳　数値.json", bb=None, rb=None, syn=250.0, games=10000, hits=40),
    rec("スマスロハナビ", "ハナビ　数値.json", bb=42, rb=41, syn=136.5, games=11330, hits=83, bbp=269.8, rbp=276.3),
]

for item in fixtures:
    path = ROOT / item["source_file"]
    path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", path.name)
print("ok")
