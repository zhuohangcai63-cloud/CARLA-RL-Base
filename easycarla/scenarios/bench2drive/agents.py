"""Ownership bridge for CARLA's bundled constant-velocity agent.

The bundled implementation creates an unregistered collision sensor and changes
velocity inside its callback thread. These overrides retain its driving logic
but schedule actuator writes through the same gateway as every other source.
"""
import queue

import carla
from agents.navigation.constant_velocity_agent import ConstantVelocityAgent as CarlaConstantVelocityAgent

from .native_control import _active, native_actor_call


class ConstantVelocityAgent(CarlaConstantVelocityAgent):
    def __init__(self, *args, **kwargs):
        self._easycarla_state = _active.get()
        if self._easycarla_state is None:
            raise RuntimeError('Native constant-velocity agent requires an episode control scope')
        super().__init__(*args, **kwargs)

    def _set_constant_velocity(self, speed):
        native_actor_call(self._vehicle, 'enable_constant_velocity', carla.Vector3D(speed, 0, 0))

    def stop_constant_velocity(self):
        self.is_constant_velocity_active = False
        native_actor_call(self._vehicle, 'disable_constant_velocity')
        self._constant_velocity_stop_time = self._world.get_snapshot().timestamp.elapsed_seconds

    def _set_collision_sensor(self):
        state = self._easycarla_state
        world = state['world']
        blueprint = world.get_blueprint_library().find('sensor.other.collision')
        self._collision_sensor = world.spawn_actor(blueprint, carla.Transform(), attach_to=self._vehicle)
        def collision(event):
            if state.get('closed'):
                return
            try:
                state['native_events'].put_nowait((self._vehicle.id, self.stop_constant_velocity))
            except queue.Full:
                state['callback_fault'] = 'Native collision callback queue exceeded 4096 entries'
        self._collision_sensor.listen(collision)

    def destroy_sensor(self):
        if self._collision_sensor is not None:
            self._collision_sensor.stop()
            native_actor_call(self._collision_sensor, 'destroy')
            self._collision_sensor = None
