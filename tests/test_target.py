"""Tests for the Target tracker page.

The reference numbers in TargetPlanTest are copied from the 'Target' sheet of
DreamProject.xlsx (Srinivas, 10,000 capital compounding at 1% per day; daily
profit/loss entries 200 / -50 / 30 on days 1-3):
day 1 target profit 100, closing 10,100, net target 100
day 2 target profit 101, closing 10,201, net target 201
day 3 target profit 102.01, closing 10,303.01, net target 303.01
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as viewer
from werkzeug.datastructures import MultiDict

# Hermetic database: never touch the real target.db while testing.
viewer.TARGET_DB_PATH = os.path.join(
    tempfile.mkdtemp(prefix='nse-target-'), 'target.db')

START = datetime(2025, 2, 18).date()
CAPITAL = 10000


class TargetPlanTest(unittest.TestCase):
    def assertMoney(self, actual, expected):
        """Money can drift by a few micro-units through compounding."""
        self.assertAlmostEqual(actual, expected, places=4)

    def plan(self, pnls=None, days=4):
        return viewer.compute_target_plan(
            CAPITAL, 1, START, days, pnls if pnls is not None else []
        )

    def test_target_side_matches_sheet(self):
        rows, _ = self.plan()
        expected = [
            # t_invest, t_profit, t_close, t_net
            (10000.0, 100.0, 10100.0, 100.0),
            (10100.0, 101.0, 10201.0, 201.0),
            (10201.0, 102.01, 10303.01, 303.01),
            (10303.01, 103.0301, 10406.0401, 406.0401),
        ]
        for row, (invest, profit, close, net) in zip(rows, expected):
            self.assertMoney(row["t_invest"], invest)
            self.assertMoney(row["t_profit"], profit)
            self.assertMoney(row["t_close"], close)
            self.assertMoney(row["t_net"], net)

    def test_daily_pnl_accumulates_and_difference_matches_sheet(self):
        rows, _ = self.plan([200, -50, 30])
        expected = [
            # today_target, pnl, net_pnl, diff
            (100.0, 200.0, 200.0, 100.0),
            (1.0, -50.0, 150.0, -51.0),
            (153.01, 30.0, 180.0, -123.01),
        ]
        for row, (target, pnl, net, diff) in zip(rows[:3], expected):
            self.assertMoney(row["today_target"], target)
            self.assertMoney(row["pnl"], pnl)
            self.assertMoney(row["net_pnl"], net)
            self.assertMoney(row["diff"], diff)

        # Day 4 has no entry: net P/L and difference stay blank, but Today
        # Target still carries the gap forward (E4 - K3).
        self.assertIsNone(rows[3]["pnl"])
        self.assertIsNone(rows[3]["net_pnl"])
        self.assertIsNone(rows[3]["diff"])
        self.assertMoney(rows[3]["today_target"], 103.0301 + 123.01)

    def test_today_target_is_day_profit_before_any_entry(self):
        rows, _ = self.plan([])
        for row in rows:
            self.assertMoney(row["today_target"], row["t_profit"])

    def test_dates_are_consecutive_days(self):
        rows, _ = self.plan()
        self.assertEqual([r["date"] for r in rows], [
            "18/02/2025", "19/02/2025", "20/02/2025", "21/02/2025",
        ])

    def test_no_entries_leaves_progress_blank(self):
        rows, summary = self.plan([], days=10)
        self.assertTrue(all(r["pnl"] is None for r in rows))
        self.assertTrue(all(r["net_pnl"] is None for r in rows))
        self.assertTrue(all(r["diff"] is None for r in rows))
        self.assertIsNone(summary["net_pnl"])
        self.assertIsNone(summary["difference"])
        self.assertEqual(summary["next_day"], 1)
        # 18/02/2025 is a Tuesday: days 5-6 are the weekend, so the balance
        # only compounds on the other 8 of the 10 days.
        self.assertTrue(rows[4]["holiday"] and rows[5]["holiday"])
        self.assertEqual(summary["holiday_count"], 2)
        self.assertEqual(summary["trading_days"], 8)
        self.assertMoney(rows[-1]["t_close"], CAPITAL * 1.01 ** 8)

    def test_weekends_are_holidays_by_default(self):
        # 22/02/2025 is a Saturday.
        rows, _ = viewer.compute_target_plan(
            CAPITAL, 1, datetime(2025, 2, 22).date(), 3, [])
        self.assertTrue(rows[0]["holiday"])
        self.assertTrue(rows[1]["holiday"])
        self.assertFalse(rows[2]["holiday"])
        self.assertTrue(rows[0]["weekend"])
        self.assertIsNone(rows[0]["t_profit"])
        self.assertIsNone(rows[0]["today_target"])
        self.assertMoney(rows[0]["t_close"], CAPITAL)    # balance does not grow
        self.assertMoney(rows[2]["t_invest"], CAPITAL)   # Monday starts from there
        self.assertMoney(rows[2]["t_profit"], 100.0)
        self.assertMoney(rows[2]["today_target"], 100.0)

    def test_marked_holiday_skips_one_day(self):
        wed = datetime(2025, 2, 19).date()
        rows, _ = viewer.compute_target_plan(
            CAPITAL, 1, START, 4, [], holidays=[wed])
        self.assertFalse(rows[0]["holiday"])
        self.assertTrue(rows[1]["holiday"])              # Wednesday marked
        self.assertMoney(rows[1]["t_close"], 10100.0)    # holds Tuesday's balance
        self.assertMoney(rows[2]["t_invest"], 10100.0)   # Thursday carries over
        self.assertMoney(rows[2]["t_profit"], 101.0)     # one day lost, not two
        self.assertMoney(rows[3]["t_profit"], 102.01)    # compounding resumes

    def test_pnl_is_ignored_on_a_holiday(self):
        wed = datetime(2025, 2, 19).date()
        rows, summary = viewer.compute_target_plan(
            CAPITAL, 1, START, 3, [200, 30, 30], holidays=[wed])
        self.assertMoney(rows[0]["net_pnl"], 200.0)
        self.assertIsNone(rows[1]["net_pnl"])            # holiday entry ignored
        self.assertIsNone(rows[1]["pnl"])
        self.assertMoney(rows[2]["net_pnl"], 230.0)      # 200 + 30, gap dropped
        self.assertEqual(summary["holiday_count"], 1)

    def test_gap_entry_is_allowed_and_net_keeps_running(self):
        rows, _ = self.plan([200, None, 30], days=3)
        self.assertIsNone(rows[1]["net_pnl"])            # blank on a skipped day
        self.assertIsNone(rows[1]["diff"])
        self.assertMoney(rows[2]["net_pnl"], 230.0)      # resumes from day 1 total
        self.assertMoney(rows[2]["today_target"], 102.01 - 100.0)  # gap carried forward

    def test_projection_matches_compounding_formula(self):
        _, summary = viewer.compute_target_plan(
            CAPITAL, 1, START, 100, [], weekends_as_holidays=False)
        self.assertMoney(summary["target_close"], CAPITAL * 1.01 ** 100)
        self.assertMoney(summary["target_profit_total"],
                         CAPITAL * 1.01 ** 100 - CAPITAL)

    def test_summary_reports_gap_at_last_entry_and_next_required(self):
        _, summary = self.plan([200, -50, 30])
        self.assertEqual(summary["entry_day"], 3)
        self.assertEqual(summary["entry_date"], "20/02/2025")
        self.assertMoney(summary["net_pnl"], 180.0)
        self.assertMoney(summary["net_target_at_entry"], 303.01)
        self.assertMoney(summary["difference"], 180.0 - 303.01)
        self.assertFalse(summary["all_tracked"])
        self.assertEqual(summary["next_day"], 4)
        self.assertMoney(summary["next_today_target"], 226.0401)

    def test_summary_marks_every_day_tracked(self):
        _, summary = self.plan([1.0, 2.0], days=2)
        self.assertTrue(summary["all_tracked"])
        self.assertEqual(summary["next_day"], 2)

    def test_milestones_use_compounding(self):
        _, summary = viewer.compute_target_plan(CAPITAL, 1, START, 1, [])
        self.assertEqual([m["day"] for m in summary["milestones"]], [30, 100, 365, 1000])
        for m in summary["milestones"]:
            self.assertMoney(m["balance"], CAPITAL * 1.01 ** m["day"])


class TargetRouteTest(unittest.TestCase):
    def setUp(self):
        self.client = viewer.app.test_client()

    def test_get_renders_defaults_from_the_sheet(self):
        response = self.client.get('/target')
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('Target Tracker', body)
        self.assertIn('10,000.00', body)      # day 1 investment
        self.assertIn('>day1<', body)         # first day row
        self.assertIn(datetime.now().strftime('%d/%m/%Y'), body)
        self.assertIn('TARGET DATA', body)
        self.assertIn('ACHIEVEMENT DATA', body)
        self.assertIn('name="pnl"', body)

    def test_post_recomputes_with_entered_pnl(self):
        response = self.client.post('/target', data=MultiDict([
            ('capital', '10000'),
            ('target_pct', '2'),
            ('start_date', '2025-01-01'),
            ('days', '3'),
            ('pnl', ''),
            ('pnl', '200'),
            ('pnl', '-50'),
        ]))
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('10,404.00', body)   # day 2 closing balance
        self.assertIn('02/01/2025', body)  # consecutive dates from start date
        self.assertIn('-462.08', body)     # net 150 vs net target 612.08
        self.assertIn('value="-50"', body) # negative entries round-trip

    def test_post_rejects_bad_numbers(self):
        response = self.client.post('/target', data={
            'capital': 'abc', 'target_pct': '1',
            'start_date': '2025-02-18', 'days': '10',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('Starting capital must be a number.',
                      response.get_data(as_text=True))

    def test_post_rejects_non_numeric_pnl(self):
        response = self.client.post('/target', data=MultiDict([
            ('capital', '10000'), ('target_pct', '1'),
            ('start_date', '2025-02-18'), ('days', '10'),
            ('pnl', 'abc'),
        ]))
        self.assertIn('Profit/loss must be a number.',
                      response.get_data(as_text=True))

    def test_post_rejects_out_of_range_days(self):
        response = self.client.post('/target', data={
            'capital': '10000', 'target_pct': '1',
            'start_date': '2025-02-18', 'days': '0',
        })
        self.assertIn('Days must be between 1 and 1000.',
                      response.get_data(as_text=True))

    def test_post_saves_state_for_the_next_visit(self):
        self.client.post('/target', data=MultiDict([
            ('capital', '12345'),
            ('target_pct', '3'),
            ('start_date', '2025-03-01'),
            ('days', '5'),
            ('pnl', ''),
            ('pnl', '10'),
            ('pnl', ''),
            ('pnl', ''),
            ('pnl', ''),
        ]))
        body = self.client.get('/target').get_data(as_text=True)
        self.assertIn('value="12345"', body)   # saved capital restored
        self.assertIn('01/03/2025', body)       # saved start date restored
        self.assertIn('value="10"', body)       # saved day 2 entry restored
        self.assertIn('12,345.00', body)        # day 1 investment recomputed

    def test_save_endpoint_writes_settings_and_entries(self):
        response = self.client.post('/target/save', json={
            'capital': 10000, 'target_pct': 1,
            'start_date': '2025-02-18', 'days': 3,
            'pnls': [50, None, -20],
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json().get('ok'))
        body = self.client.get('/target').get_data(as_text=True)
        self.assertIn('value="50"', body)
        self.assertIn('value="-20"', body)
        self.assertIn('data-f="netPnl">50.00<', body)   # net after day 1

    def test_save_endpoint_rejects_bad_payload(self):
        response = self.client.post('/target/save', json={
            'capital': 10000, 'target_pct': 1,
            'start_date': '2025-02-18', 'days': 3, 'pnls': ['abc'],
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('Profit/loss', response.get_json().get('error', ''))

    def test_save_endpoint_persists_holidays(self):
        response = self.client.post('/target/save', json={
            'capital': 10000, 'target_pct': 1,
            'start_date': '2025-02-18', 'days': 7,
            'pnls': [], 'holidays': ['2025-02-20'],
        })
        self.assertEqual(response.status_code, 200)
        body = ' '.join(self.client.get('/target')
                        .get_data(as_text=True).split())
        # Thursday 20/02 marked by hand; Saturday 22/02 is a weekend default.
        self.assertIn('data-day="2" checked aria-label', body)
        self.assertIn('data-day="4" checked disabled aria-label', body)
        # The marked day shows as a holiday row in the plan.
        self.assertIn('<tr class="holiday"> <td class="fw-semibold">day3</td>', body)

    def test_save_endpoint_rejects_bad_holiday_date(self):
        response = self.client.post('/target/save', json={
            'capital': 10000, 'target_pct': 1,
            'start_date': '2025-02-18', 'days': 3,
            'pnls': [], 'holidays': ['20/02/2025'],
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('Holiday dates', response.get_json().get('error', ''))


if __name__ == '__main__':
    unittest.main()
