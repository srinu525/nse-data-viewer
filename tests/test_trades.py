"""Tests for the Today's P/L trade log on the Target page.

The trade DB is redirected to a temp file so the real target.db is never
touched while running the suite.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as viewer


class TradeRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='nse-trades-')
        viewer.TARGET_DB_PATH = os.path.join(self.tmpdir, 'target.db')
        viewer.app.config["TESTING"] = True
        self.client = viewer.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def add(self, **kwargs):
        payload = {
            'symbol': 'RELIANCE',
            'trade_type': 'intraday',
            'quantity': 1,
            'buy_price': 100,
            'charges': 10,
            'sell_price': 120,
            'sell_charges': 5,
        }
        payload.update(kwargs)
        return self.client.post('/trades/add', json=payload)

    def list_today(self):
        return self.client.get('/trades').get_json()

    def test_empty_today_returns_zero(self):
        data = self.list_today()
        self.assertEqual(data['count'], 0)
        self.assertEqual(data['total_pnl'], 0)
        self.assertEqual(data['trades'], [])

    def test_add_trade_ok(self):
        response = self.add()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['ok'], True)
        data = self.list_today()
        self.assertEqual(data['count'], 1)
        trade = data['trades'][0]
        self.assertEqual(trade['symbol'], 'RELIANCE')
        self.assertEqual(trade['trade_type'], 'intraday')
        self.assertEqual(trade['quantity'], 1)
        self.assertEqual(trade['pnl'], 5)  # (120-100)*1 - (10+5)

    def test_quantity_scales_pnl(self):
        self.add(quantity=10)
        data = self.list_today()
        self.assertEqual(data['trades'][0]['quantity'], 10)
        self.assertEqual(data['trades'][0]['pnl'], 185)   # (120-100)*10 - 15
        self.assertEqual(data['total_pnl'], 185)

    def test_quantity_defaults_to_one(self):
        self.client.post('/trades/add', json={
            'symbol': 'TCS', 'trade_type': 'delivery',
            'buy_price': 100, 'charges': 0, 'sell_price': 110, 'sell_charges': 0,
        })
        trade = self.list_today()['trades'][0]
        self.assertEqual(trade['quantity'], 1)
        self.assertEqual(trade['pnl'], 10)

    def test_add_trade_pnl_negative(self):
        self.add(buy_price=200, sell_price=150, charges=5, sell_charges=5)
        data = self.list_today()
        self.assertEqual(data['trades'][0]['pnl'], -60)
        self.assertEqual(data['total_pnl'], -60)

    def test_total_sums_trades(self):
        self.add(symbol='RELIANCE', buy_price=100, sell_price=120)
        self.add(symbol='TCS', trade_type='delivery', quantity=2,
                 buy_price=50, sell_price=40, charges=2, sell_charges=2)
        data = self.list_today()
        self.assertEqual(data['count'], 2)
        self.assertEqual([t['symbol'] for t in data['trades']], ['RELIANCE', 'TCS'])
        self.assertEqual(data['total_pnl'], -19)  # 5 + ((40-50)*2 - 4)

    def test_trades_are_date_scoped(self):
        self.add()
        with viewer._target_conn() as conn:
            conn.execute(
                'UPDATE trades SET trade_date = ?',
                (viewer.today_ist(),))  # no-op keeps them visible
        data = self.list_today()
        self.assertEqual(data['count'], 1)

    def test_list_for_other_date_is_empty(self):
        self.add()
        data = self.client.get('/trades?date=2020-01-01').get_json()
        self.assertEqual(data['count'], 0)

    def test_update_trade_ok(self):
        tid = self.add().get_json()['id']
        response = self.client.put('/trades/%d' % tid, json={
            'symbol': 'TCS',
            'trade_type': 'delivery',
            'quantity': 2,
            'buy_price': 90,
            'charges': 5,
            'sell_price': 120,
            'sell_charges': 4,
            'date': viewer.today_ist(),
        })
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['trade']['symbol'], 'TCS')
        self.assertEqual(payload['trade']['quantity'], 2)
        data = self.client.get('/trades').get_json()
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['total_pnl'], 51)  # (120-90)*2 - (5+4)

    def test_delete_trade(self):
        tid = self.add().get_json()['id']
        response = self.client.delete('/trades/%d' % tid)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.list_today()['count'], 0)

    def test_delete_missing_trade_404(self):
        response = self.client.delete('/trades/9999')
        self.assertEqual(response.status_code, 404)

    def test_add_rejects_invalid_symbol(self):
        response = self.add(symbol='BAD!SYM')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Invalid symbol', response.get_json()['error'])

    def test_add_rejects_bad_type(self):
        response = self.add(trade_type='futures')
        self.assertEqual(response.status_code, 400)
        self.assertIn('intraday or delivery', response.get_json()['error'])

    def test_add_rejects_non_numeric_price(self):
        response = self.add(buy_price='abc')
        self.assertEqual(response.status_code, 400)
        self.assertIn('numbers', response.get_json()['error'])

    def test_add_rejects_zero_price(self):
        response = self.add(sell_price=0)
        self.assertEqual(response.status_code, 400)
        self.assertIn('Buy price and sell price are required', response.get_json()['error'])

    def test_add_rejects_negative_charges(self):
        response = self.add(charges=-5)
        self.assertEqual(response.status_code, 400)
        self.assertIn('negative', response.get_json()['error'])

    def test_add_rejects_bad_quantity(self):
        for qty in ('abc', 0, -3, 1.5):
            response = self.add(quantity=qty)
            self.assertEqual(response.status_code, 400, msg='quantity=%r' % qty)

    def test_target_page_ships_trade_modal_and_qty_field(self):
        body = self.client.get('/target').get_data(as_text=True)
        self.assertIn('id="tradeModal"', body)
        self.assertIn('id="tradeSymbol"', body)
        self.assertIn('id="tradeType"', body)
        self.assertIn('id="tradeQty"', body)
        self.assertIn('class="plus-btn trade-add"', body)


if __name__ == '__main__':
    unittest.main()