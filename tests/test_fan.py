import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fan_control', ROOT/'control.py')
fan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fan)
CONFIG = fan.load_config(ROOT/'curve.json')


class Stop:
    def __init__(self, limit): self.steps=0; self.limit=limit
    def is_set(self): return self.steps>=self.limit
    def wait(self, seconds): self.steps+=1


class FanTests(unittest.TestCase):
    def test_points_and_interpolation(self):
        for temp,speed in [(20,40),(50,40),(55,50),(60,60),(65,70),(70,80),(75,90),(80,100),(85,100),(100,100)]:
            self.assertEqual(fan.target_speed(CONFIG['points'],temp),speed)

    def test_increase_immediate_decrease_delayed(self):
        curve=fan.Curve(CONFIG['points'],15)
        self.assertEqual(curve.update(85,0),100)
        self.assertEqual(curve.update(70,2),100)
        self.assertEqual(curve.update(70,16),100)
        self.assertEqual(curve.update(70,17),80)
        self.assertEqual(curve.update(80,18),100)

    def test_dry_run_never_writes_or_restores(self):
        nv=Mock();nv.temperature.return_value=85
        fan.run_curve(nv,'GPU-test',2,CONFIG,Stop(1),dry_run=True)
        nv.set_speed.assert_not_called();nv.restore.assert_not_called()

    def test_exit_restores_all_fans(self):
        nv=Mock();nv.temperature.return_value=85
        fan.run_curve(nv,'GPU-test',2,CONFIG,Stop(1))
        self.assertEqual(nv.set_speed.call_count,2)
        nv.restore.assert_called_once_with('GPU-test',2)

    def test_partial_write_failure_restores(self):
        nv=Mock();nv.temperature.return_value=85
        nv.set_speed.side_effect=[None,fan.NVError('not supported')]
        with self.assertRaises(fan.NVError):fan.run_curve(nv,'GPU-test',2,CONFIG,Stop(1))
        nv.restore.assert_called_once_with('GPU-test',2)

    def test_sensor_failure_restores(self):
        nv=Mock();nv.temperature.side_effect=[85,fan.NVError('sensor failed')]
        with self.assertRaises(fan.NVError):fan.run_curve(nv,'GPU-test',1,CONFIG,Stop(2))
        nv.restore.assert_called_once_with('GPU-test',1)

    def test_invalid_curve_rejected(self):
        for points in [[[50,40],[40,80]],[[50,80],[60,40]],[[50,40],[80,101]],[[50,float('nan')],[80,100]]]:
            with self.subTest(points=points),tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp)/'curve.json';p.write_text(json.dumps(dict(CONFIG,points=points)))
                with self.assertRaises(ValueError):fan.load_config(p)

    def test_auto_restore_attempts_remaining_fans(self):
        nv=fan.NVML.__new__(fan.NVML)
        nv.call=Mock(side_effect=[fan.NVError('failed'),None])
        with self.assertRaises(fan.NVError):nv.restore('GPU-test',2)
        self.assertEqual(nv.call.call_count,2)


if __name__=='__main__':unittest.main()
