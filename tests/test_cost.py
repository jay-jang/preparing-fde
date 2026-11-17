from dataclasses import FrozenInstanceError
from decimal import Decimal
from fractions import Fraction
import unittest

from fde.cost import (
    gpu_seconds_per_answer, krw_per_busy_gpu_second, node_monthly_cost, staff_monthly_cost,
    unit_cost,
)

MONTH_SECONDS = 720 * 3_600     # 30 days x 24 hours, placeholder month


def node(**updates):
    args = dict(hardware_krw=72_000_000, amortization_months=36, average_power_w=1_000,
                hours_per_month=720, krw_per_kwh=200, pue=Decimal('1.5'),
                support_yearly_krw=12_000_000)
    args.update(updates)
    return node_monthly_cost(**args)


def unit(answered, **updates):
    args = dict(node_krw=3_216_000, shared_krw=2_000_000, answered_per_month=answered,
                gpu_seconds_per_answer=Decimal('0.5'), seconds_per_month=MONTH_SECONDS,
                max_utilization=Decimal('0.5'))
    args.update(updates)
    return unit_cost(**args)


class FixedCostTests(unittest.TestCase):
    def test_placeholder_node_month(self):
        cost = node()
        self.assertEqual((cost.hardware_krw, cost.power_krw, cost.support_krw),
                         (2_000_000, 216_000, 1_000_000))
        self.assertEqual(cost.total_krw, 3_216_000)
        self.assertIs(type(cost.total_krw), int)

    def test_power_is_kwh_times_price_times_pue(self):
        self.assertEqual(node(pue=1).power_krw, 144_000)            # 720 kWh x 200
        self.assertEqual(node(pue=Decimal('1.5')).power_krw, 216_000)
        self.assertEqual(node(pue=Fraction(3, 2)).power_krw, 216_000)

    def test_monthly_components_round_up(self):
        cost = node(hardware_krw=100, amortization_months=3, average_power_w=1,
                    hours_per_month=1, krw_per_kwh=1, support_yearly_krw=13)
        self.assertEqual((cost.hardware_krw, cost.power_krw, cost.support_krw), (34, 1, 2))
        self.assertEqual(staff_monthly_cost(10, Decimal('0.25')), 3)

    def test_staff_share(self):
        self.assertEqual(staff_monthly_cost(10_000_000, Decimal('0.2')), 2_000_000)
        self.assertEqual(staff_monthly_cost(10_000_000, 1), 10_000_000)
        with self.assertRaises(ValueError):
            staff_monthly_cost(10_000_000, Decimal('1.01'))


class UnitCostTests(unittest.TestCase):
    def test_cost_per_answer_falls_with_volume(self):
        expected = {10_000: (1, 5_216_000, '521.60', '1043.20'),
                    100_000: (1, 5_216_000, '52.16', '104.32'),
                    1_000_000: (1, 5_216_000, '5.22', '10.43'),
                    3_000_000: (2, 8_432_000, '2.81', '5.62')}
        for answered, (nodes, total, per_answer, per_second) in expected.items():
            with self.subTest(answered=answered):
                cost = unit(answered)
                self.assertEqual((cost.nodes, cost.monthly_total_krw), (nodes, total))
                self.assertEqual(cost.krw_per_answer, Decimal(per_answer))
                self.assertEqual(cost.krw_per_busy_gpu_second, Decimal(per_second))

    def test_gpu_seconds_set_the_node_count_not_the_unit_price(self):
        # all cost is fixed, so with the same node count the price is total / answered
        for seconds in (Decimal('0.25'), Decimal('0.5'), 1):
            with self.subTest(seconds=seconds):
                cost = unit(100_000, gpu_seconds_per_answer=seconds)
                self.assertEqual((cost.nodes, cost.krw_per_answer), (1, Decimal('52.16')))
        fewer = unit(3_000_000, gpu_seconds_per_answer=Decimal('0.25'))
        self.assertEqual((fewer.nodes, fewer.monthly_total_krw), (1, 5_216_000))
        self.assertEqual(fewer.krw_per_answer, Decimal('1.74'))
        self.assertEqual(unit(3_000_000).nodes, 2)

    def test_exact_utilization(self):
        self.assertEqual(unit(100_000).utilization, Fraction(25, 1_296))
        self.assertEqual(unit(1_000_000).utilization, Fraction(125, 648))
        self.assertEqual(round(float(unit(1_000_000).utilization), 4), 0.1929)
        self.assertEqual(unit(3_000_000).utilization, Fraction(1_500_000, 2 * MONTH_SECONDS))

    def test_node_step_at_the_utilization_ceiling(self):
        self.assertEqual(unit(2_592_000).nodes, 1)
        self.assertEqual(unit(2_592_000).utilization, Fraction(1, 2))
        self.assertEqual(unit(2_592_001).nodes, 2)
        self.assertEqual(unit(100_000, gpus_per_node=2).utilization, Fraction(25, 2_592))

    def test_measured_inputs_are_consistent(self):
        per_answer = gpu_seconds_per_answer(50_000, 100_000)
        self.assertEqual(per_answer, Fraction(1, 2))
        cost = unit(100_000, gpu_seconds_per_answer=per_answer)
        self.assertEqual(cost.krw_per_answer, Decimal('52.16'))
        self.assertEqual(krw_per_busy_gpu_second(5_216_000, MONTH_SECONDS, cost.utilization),
                         cost.krw_per_busy_gpu_second)
        self.assertEqual(gpu_seconds_per_answer(1, 3), Fraction(1, 3))

    def test_half_up_rounding_to_one_hundredth_won(self):
        self.assertEqual(unit(200, node_krw=1, shared_krw=0).krw_per_answer, Decimal('0.01'))
        self.assertEqual(unit(201, node_krw=1, shared_krw=0).krw_per_answer, Decimal('0.00'))
        self.assertEqual(str(unit(10_000).krw_per_answer), '521.60')


class ValidationTests(unittest.TestCase):
    def test_floats_are_refused_for_money_ratios(self):
        with self.assertRaises(TypeError):
            node(pue=1.5)
        with self.assertRaises(TypeError):
            staff_monthly_cost(10_000_000, 0.2)
        for field in ('gpu_seconds_per_answer', 'max_utilization'):
            with self.subTest(field=field):
                with self.assertRaises(TypeError):
                    unit(100_000, **{field: 0.5})
        with self.assertRaises(TypeError):
            gpu_seconds_per_answer(0.5, 1)

    def test_out_of_range_values_fail_closed(self):
        for updates in (dict(pue=Decimal('0.9')), dict(pue=Decimal('NaN')),
                        dict(amortization_months=0), dict(hardware_krw=-1),
                        dict(average_power_w=True), dict(hours_per_month=720.0)):
            with self.subTest(updates=updates):
                with self.assertRaises((ValueError, TypeError)):
                    node(**updates)
        for updates in (dict(max_utilization=Decimal('1.2')), dict(max_utilization=0),
                        dict(gpu_seconds_per_answer=Decimal('-1')), dict(seconds_per_month=0),
                        dict(gpus_per_node=0), dict(node_krw=-1)):
            with self.subTest(updates=updates):
                with self.assertRaises(ValueError):
                    unit(100_000, **updates)
        for answered in (0, -1, True, 1.0):
            with self.subTest(answered=answered):
                with self.assertRaises(ValueError):
                    unit(answered)
                with self.assertRaises(ValueError):
                    gpu_seconds_per_answer(1, answered)

    def test_results_are_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            node().power_krw = 0
        with self.assertRaises(FrozenInstanceError):
            unit(100_000).nodes = 3


if __name__ == '__main__':
    unittest.main()
