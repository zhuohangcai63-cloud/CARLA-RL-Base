"""Opt-in actual native-role handoff through the public runtime extension."""
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get('EASYCARLA_B2D_TAKEOVER_ROUTE'),
                                reason='requires an explicitly assigned native route and live server')


def test_native_role_takeover_and_switch():
    import numpy as np
    from easycarla.runtime import EnvironmentRuntime
    from easycarla.observations.geometry import xyz
    from scripts.validate_routes import reference_control
    route = os.environ['EASYCARLA_B2D_TAKEOVER_ROUTE']
    root = Path(__file__).resolve().parents[1]
    runtime = EnvironmentRuntime({'simulation': {'port': int(os.environ.get('EASYCARLA_B2D_PORT', '2100')),
                                                  'tm_port': int(os.environ.get('EASYCARLA_B2D_TM_PORT', '8100')),
                                                  'timeout': 90, 'reload_on_reset': False,
                                                  'no_rendering_mode': True, 'map_layers': 'none',
                                                  'tile_stream_distance': 500, 'actor_active_distance': 500},
                                  'scenario': {'source': 'bench2drive', 'purpose': 'eval',
                                               'manifest': str(root / 'configs/manifests/v004_eval220.json'),
                                               'allow_unvalidated_takeover': True},
                                  'experiments': {'enabled': True},
                                  'task': {'profile': 'bench2drive_native', 'max_seconds': 120}})
    report = {'route_id': route, 'status': 'running', 'takeover': None,
              'simulation_config': runtime.config['simulation'],
              'scope': 'one route/role/phase and short handoff window; no lifecycle extension or replay guarantee'}
    output = root / f'docs/testing/platform-takeover-{route}.json'
    try:
        obs, info = runtime.reset(seed=123, options={'route_id': route})
        report['metadata'] = info['scenario']
        selected = None
        for step in range(250):
            roles = runtime.capabilities()['scenario']['takeover_roles']
            selected = next((role for role, value in roles.items() if value['can_takeover']), None)
            if selected is not None:
                report['phase'] = roles[selected]['phase']
                report['handoff_frame'] = int(obs['frame'])
                report['takeover'] = runtime.experiment('takeover', role=selected, mechanism='L')
                break
            obs, _, terminated, truncated, info = runtime.step(reference_control(obs))
            if terminated or truncated: break
        assert selected is not None, 'Route never entered a supported handoff phase'
        actor, controller = runtime.episode.controllers[selected]
        origin = xyz(actor.get_location())
        samples = []
        for step in range(12):
            if step == 6:
                report['switch'] = runtime.experiment('set_mechanism', role=selected, mechanism='G')
            obs, _, terminated, truncated, info = runtime.step(reference_control(obs))
            samples.append({'frame': int(obs['frame']), 'position': xyz(actor.get_location()).tolist(),
                            'owner': runtime.session.controls.owner(actor.id),
                            'mechanism': controller.mechanism_name,
                            'control': [actor.get_control().steer, actor.get_control().throttle, actor.get_control().brake]})
            if terminated or truncated: break
        displacement = float(np.linalg.norm((xyz(actor.get_location()) - origin)[:2]))
        assert displacement > 0.2, 'Replacement never produced horizontal movement'
        assert any(sample['control'][1] > .05 for sample in samples), 'Replacement never applied throttle'
        assert any(sample['mechanism'] == 'G' for sample in samples)
        assert all(sample['owner'] != 'bench2drive/native' for sample in samples)
        report.update(status='passed', displacement_m=displacement, samples=samples,
                      execution_ticks=int(obs['frame']) - report['handoff_frame'],
                      execution_seconds=(int(obs['frame']) - report['handoff_frame']) * runtime.config['simulation']['physics_dt'],
                      native_suppression=info['events']['native_control_suppressed'])
    except Exception as exc:
        import traceback
        report.update(status='failed', error=str(exc), traceback=traceback.format_exc())
        raise
    finally:
        try:
            runtime.shutdown()
        except Exception as exc:
            report.update(status='failed', cleanup_error=str(exc))
        finally:
            output.write_text(json.dumps(report, indent=2) + '\n')
    assert report['status'] == 'passed', report
