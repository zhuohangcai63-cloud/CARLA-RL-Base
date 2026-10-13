"""Opt-in real actuator checks for named native handoffs, without a model."""
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get('EASYCARLA_B2D_OPERATIONS') != '1',
                                reason='requires a dedicated live native-control test server')


def test_native_velocity_impulse_and_constant_velocity_handoff():
    from easycarla.config import validate_config
    from easycarla.control.ownership import ControlConflict
    from easycarla.scenarios.bench2drive.native_control import control_scope, native_actor_call
    from easycarla.simulation.session import WorldSession
    config = validate_config({'simulation': {'port': int(os.environ.get('EASYCARLA_B2D_PORT', '2300')),
                                             'tm_port': int(os.environ.get('EASYCARLA_B2D_TM_PORT', '8300')),
                                             'no_rendering_mode': True, 'map_layers': 'none',
                                             'tile_stream_distance': 500, 'actor_active_distance': 500,
                                             'reload_on_reset': False}})
    session = WorldSession(config)
    report = {'scope': 'controlled native actuator operation; does not claim a natural scenario trigger',
              'status': 'running', 'config': config['simulation']}
    output = Path(__file__).resolve().parents[1] / 'docs/testing/platform-p4-control-operations.json'
    try:
        session.load('Town05', 123)
        blueprint = session.world.get_blueprint_library().find('vehicle.tesla.model3')
        actor = next((a for p in session.map.get_spawn_points()
                      if (a := session.world.try_spawn_actor(blueprint, p)) is not None), None)
        assert actor is not None
        session.registry.register(actor, 'native_operation_fixture')
        session.controls.claim(actor.id, 'bench2drive/native', mode='delegated')
        state = {'controls': session.controls, 'owner': 'bench2drive/native', 'world': session.world,
                 'leased': set(), 'suppressed': {}}
        with control_scope(state):
            native_actor_call(actor, 'set_autopilot', True, session.sim['tm_port'])
        session.tick(phase='initialization')
        frame = session.world.get_snapshot().frame
        with control_scope(state):
            with pytest.raises(ControlConflict):
                native_actor_call(actor, 'set_target_velocity', session.carla.Vector3D())
            native_actor_call(actor, 'apply_native_velocity_impulse', session.carla.Vector3D())
        session.tick()
        assert session.world.get_snapshot().frame == frame + 1
        assert session.controls.export_state({actor.id: 'test'})['owners']['test']['mode'] == 'manual'
        report['impulse_speed_m_s'] = actor.get_velocity().length()
        assert actor.id in state['resume_autopilot']
        with control_scope(state):
            native_actor_call(actor, 'set_autopilot', False, session.sim['tm_port'])
        assert actor.id not in state['resume_autopilot']
        session.tick()
        assert session.controls.export_state({actor.id: 'test'})['owners']['test']['mode'] == 'manual'
        with control_scope(state):
            native_actor_call(actor, 'enable_constant_velocity', session.carla.Vector3D(4, 0, 0))
        session.tick()
        initial_speed = actor.get_velocity().length()
        assert initial_speed > 3.5
        session.controls.handoff(actor, 'replacement', expected_owner='bench2drive/native')
        state['leased'].add(actor.id)
        with control_scope(state):
            native_actor_call(actor, 'set_autopilot', True, session.sim['tm_port'])
            native_actor_call(actor, 'enable_constant_velocity', session.carla.Vector3D(20, 0, 0))
        for _ in range(20):
            session.controls.apply_control(actor, [0, 0, 1], owner='replacement')
            session.tick()
        final_speed = actor.get_velocity().length()
        assert final_speed < initial_speed * .5
        assert state['suppressed'][actor.id] == 2
        assert session.controls.owner(actor.id) == 'replacement'
        report.update(status='passed', constant_velocity_initial_speed_m_s=initial_speed,
                      replacement_braking_final_speed_m_s=final_speed,
                      old_source_commands_suppressed=state['suppressed'][actor.id], ticks=dict(session.ticks))
    except Exception as exc:
        import traceback
        report.update(status='failed', error=str(exc), traceback=traceback.format_exc())
        raise
    finally:
        try: session.close()
        except Exception as exc: report.update(status='failed', cleanup_error=str(exc))
        output.write_text(json.dumps(report, indent=2) + '\n')
    assert report['status'] == 'passed', report
