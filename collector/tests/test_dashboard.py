import json
from pathlib import Path

from dashboard import generate_dashboard, write_dashboard


class TestGenerateDashboard:
    def test_returns_valid_json_structure(self):
        d = generate_dashboard(["Room A", "Room B"])
        assert d["title"] == "Ruuvi Dashboard"
        assert d["uid"] == "ruuvi-sensors"
        assert "panels" in d
        assert d["templating"] == {"list": []}

    def test_custom_title(self):
        d = generate_dashboard(["A"], title="My Weather")
        assert d["title"] == "My Weather"

    def test_temperature_stat_panels_match_tag_order(self):
        d = generate_dashboard(["Zebra", "Alpha", "Middle"])
        temp_stats = [
            p
            for p in d["panels"]
            if p["type"] == "stat" and "temperature" in p["targets"][0]["expr"]
        ]
        assert len(temp_stats) == 3
        assert temp_stats[0]["title"] == "Zebra"
        assert temp_stats[1]["title"] == "Alpha"
        assert temp_stats[2]["title"] == "Middle"

    def test_humidity_stat_panels_match_tag_order(self):
        d = generate_dashboard(["A", "B"])
        hum_stats = [
            p
            for p in d["panels"]
            if p["type"] == "stat" and "humidity" in p["targets"][0]["expr"]
        ]
        assert len(hum_stats) == 2
        assert hum_stats[0]["title"] == "A"
        assert hum_stats[1]["title"] == "B"

    def test_empty_tags_produces_no_stat_panels(self):
        d = generate_dashboard([])
        stat_panels = [p for p in d["panels"] if p["type"] == "stat"]
        assert len(stat_panels) == 0

    def test_history_and_extra_panels_always_present(self):
        d = generate_dashboard([])
        timeseries = [p for p in d["panels"] if p["type"] == "timeseries"]
        assert len(timeseries) == 7

    def test_grid_positions_no_overlap(self):
        d = generate_dashboard(["A", "B", "C", "D", "E", "F", "G", "H", "I"])
        occupied = set()
        for p in d["panels"]:
            gp = p["gridPos"]
            for dx in range(gp["w"]):
                for dy in range(gp["h"]):
                    cell = (gp["x"] + dx, gp["y"] + dy)
                    assert cell not in occupied, (
                        f"Panel '{p.get('title')}' overlaps at {cell}"
                    )
                    occupied.add(cell)

    def test_stat_panel_width_default_6_columns(self):
        d = generate_dashboard(["A", "B", "C"])
        stats = [p for p in d["panels"] if p["type"] == "stat"]
        for s in stats:
            assert s["gridPos"]["w"] == 4  # 24 // 6

    def test_stat_panel_width_adapts_to_columns(self):
        d = generate_dashboard(["A", "B"], columns_per_row=4)
        stats = [p for p in d["panels"] if p["type"] == "stat"]
        for s in stats:
            assert s["gridPos"]["w"] == 6  # 24 // 4

    def test_default_6_panels_per_row(self):
        d = generate_dashboard(["A", "B", "C", "D", "E", "F", "G"])
        temp_stats = [
            p
            for p in d["panels"]
            if p["type"] == "stat" and "temperature" in p["targets"][0]["expr"]
        ]
        row1_y = temp_stats[0]["gridPos"]["y"]
        row1 = [p for p in temp_stats if p["gridPos"]["y"] == row1_y]
        row2 = [p for p in temp_stats if p["gridPos"]["y"] == row1_y + 3]
        assert len(row1) == 6
        assert len(row2) == 1

    def test_custom_columns_per_row(self):
        d = generate_dashboard(["A", "B", "C", "D", "E"], columns_per_row=3)
        temp_stats = [
            p
            for p in d["panels"]
            if p["type"] == "stat" and "temperature" in p["targets"][0]["expr"]
        ]
        row1_y = temp_stats[0]["gridPos"]["y"]
        row1 = [p for p in temp_stats if p["gridPos"]["y"] == row1_y]
        row2 = [p for p in temp_stats if p["gridPos"]["y"] == row1_y + 3]
        assert len(row1) == 3
        assert len(row2) == 2


class TestWriteDashboard:
    def test_writes_json_file(self, tmp_path):
        path = str(tmp_path / "ruuvi.json")
        write_dashboard(["Tag A"], path)
        d = json.loads(Path(path).read_text())
        assert d["uid"] == "ruuvi-sensors"
        assert any(p.get("title") == "Tag A" for p in d["panels"])

    def test_overwrites_existing(self, tmp_path):
        path = str(tmp_path / "ruuvi.json")
        write_dashboard(["Old"], path)
        write_dashboard(["New"], path)
        d = json.loads(Path(path).read_text())
        titles = [p.get("title") for p in d["panels"]]
        assert "New" in titles
        assert "Old" not in titles
