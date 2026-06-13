# -*- coding: utf-8 -*-
"""
Author: SilverWings (Modified with fixes v3)
GitHub: https://github.com/silverwingsbot

修复内容 v3：
1. 优化行人生成策略，提高成功率
2. 修复 "actor already dead" 警告
3. 添加更健壮的错误处理
"""

from __future__ import division
import numpy as np
import random
import time
import gymnasium as gym
import carla
import sys

# 将 CARLA 的 PythonAPI 文件夹路径添加到系统路径中
correct_path = r'/home/czh/software/carla/PythonAPI/carla'
if correct_path not in sys.path:
    sys.path.append(correct_path)

from agents.navigation.global_route_planner import GlobalRoutePlanner  # type:ignore
from easycarla.scenarios import ScenarioSpec, build_scenario_provider
from easycarla.scenarios.long_tail_events import LongTailEventManager

DEFAULT_NPC_VEHICLE_BLACKLIST = {
    'vehicle.carlamotors.carlacola',
    'vehicle.carlamotors.firetruck',
    'vehicle.ford.ambulance',
    'vehicle.mercedes.sprinter',
    'vehicle.mitsubishi.fusorosa',
    'vehicle.volkswagen.t2',
    'vehicle.volkswagen.t2_2021',
}


class CarlaEnv(gym.Env):
    def __init__(self, params):
        self.enable_camera = params.get('enable_camera', True)
        self.enable_lidar = params.get('enable_lidar', True)
        self.enable_bev = params.get('enable_bev', True)
        self.collision_sensor = None
        self.lidar_sensor = None
        self.camera_sensor = None
        self.bev_semantic_sensor = None
        self._is_collision = False
        self._is_off_road = False
        self.off_road_counter = 0
        self.number_of_vehicles = params['number_of_vehicles']
        self.number_of_walkers = params['number_of_walkers']
        self.town_name = params['town']
        self.port = params['port']
        self.dt = params['dt']
        self.max_time_episode = params['max_time_episode']
        self.max_waypoints = params['max_waypoints']
        self.visualize_waypoints = params['visualize_waypoints']
        self.desired_speed = params['desired_speed']
        self.max_ego_spawn_times = params['max_ego_spawn_times']
        self.ego_autopilot = bool(params.get('ego_autopilot', False))
        self.view_mode = params['view_mode']
        self.traffic = params['traffic']
        self.lidar_max_range = params['lidar_max_range']
        self.image_size_x = params.get('image_size_x', 84)
        self.image_size_y = params.get('image_size_y', 84)
        self.bev_image_size_x = params.get('bev_image_size_x', 84)
        self.bev_image_size_y = params.get('bev_image_size_y', 84)
        self.bev_view_height = params.get('bev_view_height', 25.0)
        self.max_nearby_vehicles = params['max_nearby_vehicles']
        self.surrounding_vehicle_spawned_randomly = params['surrounding_vehicle_spawned_randomly']
        self.num_lookahead = params.get('num_lookahead', 20)  # 默认为 20 个点
        npc_vehicle_blacklist = params.get('npc_vehicle_blacklist', DEFAULT_NPC_VEHICLE_BLACKLIST)
        if npc_vehicle_blacklist is None:
            npc_vehicle_blacklist = DEFAULT_NPC_VEHICLE_BLACKLIST
        elif isinstance(npc_vehicle_blacklist, str):
            npc_vehicle_blacklist = [npc_vehicle_blacklist]
        self.npc_vehicle_blacklist = {
            str(vehicle_id).strip().lower()
            for vehicle_id in npc_vehicle_blacklist
            if str(vehicle_id).strip()
        }
        self.scenario_backend = params.get('scenario_backend', 'random')
        self.scenario_provider = build_scenario_provider(self.scenario_backend, params)
        self.active_scenario = ScenarioSpec(metadata={'backend': self.scenario_backend})
        raw_long_tail_events = params.get('long_tail_events', [])
        if isinstance(raw_long_tail_events, dict):
            raw_long_tail_events = [raw_long_tail_events]
        if not isinstance(raw_long_tail_events, list):
            raw_long_tail_events = []
        self.long_tail_event_specs = [
            dict(event) for event in raw_long_tail_events if isinstance(event, dict)
        ]
        self.long_tail_event_max_active = int(params.get('long_tail_event_max_active', 1))
        self.long_tail_event_max_active = max(0, self.long_tail_event_max_active)
        self.long_tail_event_seed = params.get('long_tail_event_seed', None)
        self.pedestrian_cross_factor = float(params.get('pedestrian_cross_factor', 0.2))
        self.walker_speed_min = float(params.get('walker_speed_min', 1.0))
        self.walker_speed_max = float(params.get('walker_speed_max', 2.0))
        self.forced_crossing_walkers = int(params.get('forced_crossing_walkers', 0))
        self.forced_crossing_distance_min = float(params.get('forced_crossing_distance_min', 8.0))
        self.forced_crossing_distance_max = float(params.get('forced_crossing_distance_max', 25.0))
        self.forced_crossing_lateral_min = float(params.get('forced_crossing_lateral_min', 2.0))
        self.forced_crossing_lateral_max = float(params.get('forced_crossing_lateral_max', 8.0))
        self.forced_crossing_trigger_prob = float(params.get('forced_crossing_trigger_prob', 0.03))
        self.forced_crossing_cooldown_steps = int(params.get('forced_crossing_cooldown_steps', 120))
        self.forced_crossing_warmup_steps = int(params.get('forced_crossing_warmup_steps', 40))
        self.forced_crossing_min_speed = float(params.get('forced_crossing_min_speed', 1.5))
        self.forced_crossing_max_total = int(params.get('forced_crossing_max_total', 28))
        self.lead_brake_enable_prob = float(params.get('lead_brake_enable_prob', 0.0))
        self.lead_brake_warmup_steps = int(params.get('lead_brake_warmup_steps', 50))
        self.lead_candidate_distance_min = float(params.get('lead_candidate_distance_min', 18.0))
        self.lead_candidate_distance_max = float(params.get('lead_candidate_distance_max', 45.0))
        self.lead_candidate_speed_min = float(params.get('lead_candidate_speed_min', 2.0))
        self.lead_candidate_speed_max = float(params.get('lead_candidate_speed_max', 12.0))
        self.lead_track_min_steps = int(params.get('lead_track_min_steps', 8))
        self.lead_trigger_distance_min = float(params.get('lead_trigger_distance_min', 8.0))
        self.lead_trigger_distance_max = float(params.get('lead_trigger_distance_max', 16.0))
        self.lead_brake_strength = float(params.get('lead_brake_strength', 1.0))
        self.lead_brake_hold_steps = int(params.get('lead_brake_hold_steps', 35))
        self.lead_brake_min_ego_speed = float(params.get('lead_brake_min_ego_speed', 0.0))
        self.lead_event_max_per_episode = int(params.get('lead_event_max_per_episode', 1))
        self.hard_break_route_enabled = bool(params.get('hard_break_route_enabled', False))
        self.hard_break_lead_spawn_distance_min = float(
            params.get('hard_break_lead_spawn_distance_min', 24.0)
        )
        self.hard_break_lead_spawn_distance_max = float(
            params.get('hard_break_lead_spawn_distance_max', 34.0)
        )
        if self.walker_speed_max < self.walker_speed_min:
            self.walker_speed_max = self.walker_speed_min
        if self.forced_crossing_walkers < 0:
            self.forced_crossing_walkers = 0
        if self.forced_crossing_distance_max < self.forced_crossing_distance_min:
            self.forced_crossing_distance_max = self.forced_crossing_distance_min
        if self.forced_crossing_lateral_min < 0.0:
            self.forced_crossing_lateral_min = 0.0
        if self.forced_crossing_lateral_max < self.forced_crossing_lateral_min:
            self.forced_crossing_lateral_max = self.forced_crossing_lateral_min
        self.forced_crossing_trigger_prob = max(0.0, min(1.0, self.forced_crossing_trigger_prob))
        self.forced_crossing_cooldown_steps = max(0, self.forced_crossing_cooldown_steps)
        self.forced_crossing_warmup_steps = max(0, self.forced_crossing_warmup_steps)
        self.forced_crossing_min_speed = max(0.0, self.forced_crossing_min_speed)
        self.forced_crossing_max_total = max(0, self.forced_crossing_max_total)
        self.lead_brake_enable_prob = max(0.0, min(1.0, self.lead_brake_enable_prob))
        self.lead_brake_warmup_steps = max(0, self.lead_brake_warmup_steps)
        self.lead_candidate_distance_min = max(0.0, self.lead_candidate_distance_min)
        if self.lead_candidate_distance_max < self.lead_candidate_distance_min:
            self.lead_candidate_distance_max = self.lead_candidate_distance_min
        self.lead_candidate_speed_min = max(0.0, self.lead_candidate_speed_min)
        if self.lead_candidate_speed_max < self.lead_candidate_speed_min:
            self.lead_candidate_speed_max = self.lead_candidate_speed_min
        self.lead_track_min_steps = max(1, self.lead_track_min_steps)
        self.lead_trigger_distance_min = max(0.0, self.lead_trigger_distance_min)
        if self.lead_trigger_distance_max < self.lead_trigger_distance_min:
            self.lead_trigger_distance_max = self.lead_trigger_distance_min
        self.lead_brake_strength = max(0.0, min(1.0, self.lead_brake_strength))
        self.lead_brake_hold_steps = max(1, self.lead_brake_hold_steps)
        self.lead_brake_min_ego_speed = max(0.0, self.lead_brake_min_ego_speed)
        self.lead_event_max_per_episode = max(0, self.lead_event_max_per_episode)
        self.hard_break_lead_spawn_distance_min = max(0.0, self.hard_break_lead_spawn_distance_min)
        if self.hard_break_lead_spawn_distance_max < self.hard_break_lead_spawn_distance_min:
            self.hard_break_lead_spawn_distance_max = self.hard_break_lead_spawn_distance_min
        self.current_pedestrian_cross_factor = self.pedestrian_cross_factor
        self.current_walker_speed_min = self.walker_speed_min
        self.current_walker_speed_max = self.walker_speed_max
        self.current_forced_crossing_walkers = self.forced_crossing_walkers
        self.current_forced_crossing_distance_min = self.forced_crossing_distance_min
        self.current_forced_crossing_distance_max = self.forced_crossing_distance_max
        self.current_forced_crossing_lateral_min = self.forced_crossing_lateral_min
        self.current_forced_crossing_lateral_max = self.forced_crossing_lateral_max
        self.current_forced_crossing_trigger_prob = self.forced_crossing_trigger_prob
        self.current_forced_crossing_cooldown_steps = self.forced_crossing_cooldown_steps
        self.current_forced_crossing_warmup_steps = self.forced_crossing_warmup_steps
        self.current_forced_crossing_min_speed = self.forced_crossing_min_speed
        self.current_forced_crossing_max_total = self.forced_crossing_max_total
        self.current_lead_brake_enable_prob = self.lead_brake_enable_prob
        self.current_lead_brake_warmup_steps = self.lead_brake_warmup_steps
        self.current_lead_candidate_distance_min = self.lead_candidate_distance_min
        self.current_lead_candidate_distance_max = self.lead_candidate_distance_max
        self.current_lead_candidate_speed_min = self.lead_candidate_speed_min
        self.current_lead_candidate_speed_max = self.lead_candidate_speed_max
        self.current_lead_track_min_steps = self.lead_track_min_steps
        self.current_lead_trigger_distance_min = self.lead_trigger_distance_min
        self.current_lead_trigger_distance_max = self.lead_trigger_distance_max
        self.current_lead_brake_strength = self.lead_brake_strength
        self.current_lead_brake_hold_steps = self.lead_brake_hold_steps
        self.current_lead_brake_min_ego_speed = self.lead_brake_min_ego_speed
        self.current_lead_event_max_per_episode = self.lead_event_max_per_episode
        self.current_hard_break_route_enabled = self.hard_break_route_enabled
        self.current_hard_break_lead_spawn_distance_min = self.hard_break_lead_spawn_distance_min
        self.current_hard_break_lead_spawn_distance_max = self.hard_break_lead_spawn_distance_max
        self.current_hard_break_lead_spawned = False
        self.current_hard_break_lead_spawn_failure = ""
        self.current_hard_break_lead_spawn_distance = None
        self.current_forced_crossing_spawned = 0
        self.current_forced_crossing_events = 0
        self.current_forced_crossing_completed = 0
        self.current_forced_crossing_failed = 0
        self.current_forced_crossing_timeout = 0
        self.current_lead_brake_enabled = False
        self.current_lead_brake_events = 0
        self.current_lead_brake_triggered = False
        self.current_lead_brake_completed = 0
        self.current_lead_brake_aborted = 0
        self.current_lead_brake_abort_reason = ""
        self.current_lead_brake_last_trigger_step = -1
        self.current_lead_brake_trigger_ego_speed = None
        self.current_lead_brake_last_state = "idle"
        self.current_lead_brake_last_distance = None
        self.current_lead_brake_last_relative_speed = None
        self.current_lead_brake_last_candidate_id = None
        self.hard_break_lead_vehicle_id = None
        self.last_forced_crossing_step = -10**9
        self.current_forced_crossing_disabled = False
        self.current_forced_crossing_disable_reason = ""
        self.active_lead_brake_event = None
        self.global_route = None
        self.stop_steps = 0
        self.is_red_light_present = False
        self.ego = None

        # 用于追踪生成的 walker 和 controller 的 ID（而不是对象引用）
        self.spawned_walker_ids = []
        self.spawned_controller_ids = []
        self.forced_crossing_walker_ids = []
        self.active_forced_crossing_events = {}
        self.forced_crossing_event_seq = 0
        self.spawned_vehicles = []
        self.used_spawn_points = []
        self.last_obs = None

        self.observation_space = gym.spaces.Dict({
            'lidar': gym.spaces.Box(low=0.0, high=1.0, shape=(240,), dtype=np.float32),
            'ego_state': gym.spaces.Box(low=-np.inf, high=np.inf, shape=(9,), dtype=np.float32),
            'nearby_vehicles': gym.spaces.Box(low=-np.inf, high=np.inf,
                                              shape=(self.max_nearby_vehicles * 4,), dtype=np.float32),
            'waypoints': gym.spaces.Box(low=-np.inf, high=np.inf,
                                        shape=(self.max_waypoints * 3,), dtype=np.float32),
            'lane_info': gym.spaces.Box(low=-np.inf, high=np.inf, shape=(3,), dtype=np.float32),
            'camera': gym.spaces.Box(low=0, high=255,
                                     shape=(self.image_size_y, self.image_size_x, 3), dtype=np.uint8),
            'bev_semantic': gym.spaces.Box(low=0, high=255,
                                           shape=(self.bev_image_size_y, self.bev_image_size_x, 3), dtype=np.uint8),
            'gps_targets': gym.spaces.Box(low=-np.inf, high=np.inf, shape=(self.num_lookahead, 2), dtype=np.float32),
        })
        self.action_space = gym.spaces.Box(
            low=np.array([-1.0, -1.0], dtype=np.float32),
            high=np.array([1.0, 1.0], dtype=np.float32)
        )

        # 连接到CARLA服务器并加载世界
        print('Connecting to Carla server...')
        client = carla.Client('localhost', params['port'])
        client.set_timeout(float(params.get('carla_timeout', 60.0)))
        self.client = client
        self.world = client.load_world(self.town_name)
        self.world.set_weather(carla.WeatherParameters.ClearNoon)
        print('Connection established!')

        # 准备车辆生成点
        self.vehicle_spawn_points = list(self.world.get_map().get_spawn_points())
        self.route_planner = GlobalRoutePlanner(self.world.get_map(), 1.0)

        # 行人生成点在 reset 中动态生成
        self.walker_spawn_points = []

        self.ego_bp = self._create_vehicle_bluepprint(params['ego_vehicle_filter'], color='255,0,0')

        self.collision_hist = []
        self.collision_hist_l = 1
        self.collision_bp = self.world.get_blueprint_library().find('sensor.other.collision')

        self.lidar_data = None
        self.lidar_height = 0.8
        self.lidar_trans = carla.Transform(carla.Location(x=0.0, z=self.lidar_height))
        self.lidar_bp = self.world.get_blueprint_library().find('sensor.lidar.ray_cast')
        self.lidar_bp.set_attribute('channels', '1')
        self.lidar_bp.set_attribute('range', '50')
        self.lidar_bp.set_attribute('rotation_frequency', '10')
        self.lidar_bp.set_attribute('points_per_second', '10000')
        self.lidar_bp.set_attribute('upper_fov', '0')
        self.lidar_bp.set_attribute('lower_fov', '0')

        self.camera_data = None
        #前置摄像头
        self.camera_trans = carla.Transform(carla.Location(x=1.5, z=2.4))
        #俯视角摄像头
        # self.camera_trans = carla.Transform(carla.Location(x=0.0, z=25.0), carla.Rotation(pitch=-90))
        self.camera_bp = self.world.get_blueprint_library().find('sensor.camera.rgb')
        self.camera_bp.set_attribute('image_size_x', f'{self.image_size_x}')
        self.camera_bp.set_attribute('image_size_y', f'{self.image_size_y}')
        self.camera_bp.set_attribute('fov', '90')

        self.bev_semantic_image = None  # [修复] 存储已处理的图像
        self.bev_semantic_trans = carla.Transform(
            carla.Location(x=0.0, z=self.bev_view_height),
            carla.Rotation(pitch=-90.0)
        )
        self.bev_semantic_bp = self.world.get_blueprint_library().find('sensor.camera.semantic_segmentation')
        self.bev_semantic_bp.set_attribute('image_size_x', f'{self.bev_image_size_x}')
        self.bev_semantic_bp.set_attribute('image_size_y', f'{self.bev_image_size_y}')
        self.bev_semantic_bp.set_attribute('fov', '90')

        self.settings = self.world.get_settings()
        self.settings.fixed_delta_seconds = self.dt
        self.long_tail_event_manager = LongTailEventManager(
            self,
            max_active=self.long_tail_event_max_active,
            seed=self.long_tail_event_seed,
        )

        self.reset_step = 0
        self.total_step = 0

    def _destroy_actor_ids_batch(self, actor_ids, label='actors', do_tick=True):
        """Best-effort batch destroy with filtering and error classification."""
        if not actor_ids:
            return 0

        unique_ids = []
        seen = set()
        for aid in actor_ids:
            if aid is None or aid in seen:
                continue
            seen.add(aid)
            unique_ids.append(aid)

        alive_ids = [aid for aid in unique_ids if self.world.get_actor(aid) is not None]
        if not alive_ids:
            return 0

        batch = [carla.command.DestroyActor(aid) for aid in alive_ids]
        try:
            results = self.client.apply_batch_sync(batch, do_tick)
        except RuntimeError as e:
            print(f"Warning: Failed to destroy {label}: {e}")
            return 0

        destroyed = 0
        benign_tokens = ('not found', 'already', 'non-existing', 'invalid actor')
        for aid, response in zip(alive_ids, results):
            if not response.error:
                destroyed += 1
                continue

            err = str(response.error).lower()
            if not any(token in err for token in benign_tokens):
                print(f"Warning: Failed to destroy {label} actor {aid}: {response.error}")

        return destroyed

    def _destroy_sensors(self):
        """安全地销毁所有传感器"""
        sensors = [
            self.collision_sensor,
            self.lidar_sensor,
            self.camera_sensor,
            self.bev_semantic_sensor
        ]
        sensor_ids = []

        for sensor in sensors:
            if sensor is not None:
                try:
                    if sensor.is_listening:
                        sensor.stop()
                except Exception as e:
                    print(f"Warning: Failed to destroy sensor: {e}")  # 静默处理，避免刷屏
                try:
                    if sensor.is_alive:
                        sensor_ids.append(sensor.id)
                except Exception:
                    pass

        if sensor_ids:
            self._destroy_actor_ids_batch(sensor_ids, label='sensors')

        self.collision_sensor = None
        self.lidar_sensor = None
        self.camera_sensor = None
        self.bev_semantic_sensor = None

        self.lidar_data = None
        self.camera_data = None
        self.bev_semantic_image = None  # [修复] 对应修改

    def _destroy_ego(self):
        """安全地销毁自车"""
        if self.ego is not None:
            ego_id = None
            try:
                ego_id = self.ego.id
            except Exception:
                ego_id = None
            self.ego = None
            if ego_id is not None:
                self._destroy_actor_ids_batch([ego_id], label='ego vehicle')

    def _destroy_forced_crossing_events(self, do_tick=True):
        actor_ids = list(self.forced_crossing_walker_ids)
        actor_ids.extend(event.get('walker_id') for event in self.active_forced_crossing_events.values())
        if actor_ids:
            self._destroy_actor_ids_batch(actor_ids, label='forced crossing walkers', do_tick=do_tick)
        self.forced_crossing_walker_ids = []
        self.active_forced_crossing_events = {}

    def _destroy_walkers(self):
        """
        [优化] 使用 ID 批量销毁行人，避免 "actor already dead" 警告
        """
        # 1. 先停止所有控制器
        self._destroy_forced_crossing_events(do_tick=False)
        all_actors = self.world.get_actors(self.spawned_controller_ids)
        for actor in all_actors:
            try:
                actor.stop()
            except Exception as e:
                print(f"Warning: Failed to stop walker controller {actor.id}: {e}")

        # 2. 批量销毁控制器
        if self.spawned_controller_ids:
            self._destroy_actor_ids_batch(self.spawned_controller_ids, label='walker controllers')

        # 3. 批量销毁行人
        if self.spawned_walker_ids:
            self._destroy_actor_ids_batch(self.spawned_walker_ids, label='walkers')

        # 4. 清空 ID 列表
        self.spawned_controller_ids = []
        self.spawned_walker_ids = []

    def _spawn_vehicles_batch(self, count):
        """使用批量命令生成车辆"""
        if count <= 0:
            return [], []

        spawn_points = list(self.vehicle_spawn_points)
        random.shuffle(spawn_points)
        count = min(count, len(spawn_points))

        batch = []
        blueprints = self._get_npc_vehicle_blueprints()
        if not blueprints:
            print("Warning: No NPC vehicle blueprints available after blacklist filtering")
            return [], []

        for i in range(count):
            bp = random.choice(blueprints)
            if bp.has_attribute('color'):
                color = random.choice(bp.get_attribute('color').recommended_values)
                bp.set_attribute('color', color)
            bp.set_attribute('role_name', 'autopilot')

            spawn_point = spawn_points[i]
            batch.append(carla.command.SpawnActor(bp, spawn_point).then(
                carla.command.SetAutopilot(carla.command.FutureActor, True)
            ))

        results = self.client.apply_batch_sync(batch, True)

        vehicles = []
        used_points = []
        for i, response in enumerate(results):
            if not response.error:
                vehicle = self.world.get_actor(response.actor_id)
                if vehicle is not None:
                    vehicles.append(vehicle)
                    used_points.append(spawn_points[i])

        print(f"Successfully spawned {len(vehicles)}/{count} vehicles")
        return vehicles, used_points

    def _vehicle_spawn_area_is_clear(self, location, min_distance=7.0):
        for vehicle in self.world.get_actors().filter('vehicle.*'):
            if self.ego is not None and vehicle.id == self.ego.id:
                continue
            try:
                if vehicle.get_location().distance(location) < min_distance:
                    return False
            except RuntimeError:
                continue
        return True

    def _spawn_hard_break_lead_vehicle(self):
        if not self.current_hard_break_route_enabled or self.ego is None:
            return False

        self.current_hard_break_lead_spawn_failure = ""
        world_map = self.world.get_map()
        ego_waypoint = world_map.get_waypoint(
            self.ego.get_location(), project_to_road=True, lane_type=carla.LaneType.Driving
        )
        if ego_waypoint is None or ego_waypoint.is_intersection:
            self.current_hard_break_lead_spawn_failure = "ego_not_on_driving_lane"
            return False

        distance_min = max(0.0, float(self.current_hard_break_lead_spawn_distance_min))
        distance_max = max(distance_min, float(self.current_hard_break_lead_spawn_distance_max))
        sampled_distance = random.uniform(distance_min, distance_max)
        candidate_distances = [
            sampled_distance,
            distance_min,
            distance_max,
            (distance_min + distance_max) * 0.5,
            12.0,
            16.0,
            20.0,
            24.0,
            28.0,
            32.0,
            36.0,
            42.0,
            distance_max + 5.0,
            distance_min + 5.0,
        ]

        blueprints = self._get_npc_vehicle_blueprints()
        if not blueprints:
            self.current_hard_break_lead_spawn_failure = "no_vehicle_blueprints"
            return False

        for spawn_distance in candidate_distances:
            lead_waypoint = self._advance_waypoint_along_lane(ego_waypoint, spawn_distance)
            if lead_waypoint is None or lead_waypoint.is_intersection:
                continue

            spawn_transform = carla.Transform(
                lead_waypoint.transform.location + carla.Location(z=0.35),
                lead_waypoint.transform.rotation,
            )
            if not self._vehicle_spawn_area_is_clear(spawn_transform.location):
                continue

            blueprint = random.choice(blueprints)
            if blueprint.has_attribute('color'):
                blueprint.set_attribute('color', '0,0,255')
            blueprint.set_attribute('role_name', 'hard_break_lead')

            try:
                lead_vehicle = self.world.try_spawn_actor(blueprint, spawn_transform)
            except RuntimeError as exc:
                self.current_hard_break_lead_spawn_failure = f"spawn_exception:{exc}"
                continue

            if lead_vehicle is None:
                continue

            self.hard_break_lead_vehicle_id = lead_vehicle.id
            self.current_hard_break_lead_spawned = True
            self.current_hard_break_lead_spawn_distance = float(spawn_distance)
            self.spawned_vehicles.append(lead_vehicle)

            try:
                lead_vehicle.set_autopilot(True)
            except RuntimeError as exc:
                self.current_hard_break_lead_spawn_failure = f"autopilot_enable_failed:{exc}"

            print(
                f"HardBreakRoute lead vehicle spawned id={lead_vehicle.id} "
                f"distance={spawn_distance:.1f}m"
            )
            return True

        self.current_hard_break_lead_spawn_failure = "no_clear_same_lane_spawn"
        return False

    def _get_npc_vehicle_blueprints(self):
        blueprints = self.world.get_blueprint_library().filter('vehicle.*')
        filtered = []

        for bp in blueprints:
            if not bp.has_attribute('number_of_wheels'):
                continue
            if int(bp.get_attribute('number_of_wheels')) != 4:
                continue
            if bp.id.lower() in self.npc_vehicle_blacklist:
                continue
            filtered.append(bp)

        return filtered

    def _spawn_walkers_batch(self, count):
        """
        [优化] 使用改进的批量生成策略，提高行人生成成功率
        """
        if count <= 0:
            return [], []

        # ============ 策略1: 生成更多候选位置 ============
        # 请求 2 倍的位置，因为很多会重叠或失败
        spawn_points = []
        max_attempts = count * 5  # 增加尝试次数
        attempts = 0

        # 用于检测重叠的已使用位置
        used_locations = set()
        min_distance = 2.0  # 行人之间的最小距离（米）

        while len(spawn_points) < count * 2 and attempts < max_attempts:
            loc = self.world.get_random_location_from_navigation()
            if loc is not None:
                # 检查是否与已有位置太近
                loc_tuple = (round(loc.x, 1), round(loc.y, 1))
                is_valid = True

                for used_loc in used_locations:
                    dist = np.sqrt((loc_tuple[0] - used_loc[0]) ** 2 +
                                   (loc_tuple[1] - used_loc[1]) ** 2)
                    if dist < min_distance:
                        is_valid = False
                        break

                if is_valid:
                    spawn_point = carla.Transform(location=loc)
                    spawn_points.append(spawn_point)
                    used_locations.add(loc_tuple)

            attempts += 1

        if len(spawn_points) == 0:
            print("Warning: Could not find any valid walker spawn locations")
            return [], []

        # 只取需要的数量
        spawn_points = spawn_points[:count]

        # ============ 批量生成行人 ============
        walker_bps = self.world.get_blueprint_library().filter('walker.pedestrian.*')

        batch = []
        for spawn_point in spawn_points:
            walker_bp = random.choice(walker_bps)
            if walker_bp.has_attribute('is_invincible'):
                walker_bp.set_attribute('is_invincible', 'false')
            batch.append(carla.command.SpawnActor(walker_bp, spawn_point))

        results = self.client.apply_batch_sync(batch, True)

        walker_ids = []
        for response in results:
            if not response.error:
                walker_ids.append(response.actor_id)

        # ============ 批量生成控制器 ============
        walker_controller_bp = self.world.get_blueprint_library().find('controller.ai.walker')
        batch = []
        for walker_id in walker_ids:
            batch.append(carla.command.SpawnActor(walker_controller_bp, carla.Transform(), walker_id))

        results = self.client.apply_batch_sync(batch, True)

        controller_ids = []
        for response in results:
            if not response.error:
                controller_ids.append(response.actor_id)

        # ============ 启动控制器 ============
        self.world.tick()

        all_controllers = self.world.get_actors(controller_ids)
        for controller in all_controllers:
            try:
                controller.start()
                controller.go_to_location(self.world.get_random_location_from_navigation())
                controller.set_max_speed(random.uniform(self.current_walker_speed_min, self.current_walker_speed_max))
            except Exception as e:
                print(f"Warning: Failed to start walker controller {controller.id}: {e}")

        # 保存 ID 用于后续销毁
        self.spawned_walker_ids.extend(walker_ids)
        self.spawned_controller_ids.extend(controller_ids)

        print(f"Successfully spawned {len(walker_ids)}/{count} walkers")
        return walker_ids, controller_ids

    def _world_to_ego_local(self, ego_transform, location):
        ego_loc = ego_transform.location
        ego_yaw = np.deg2rad(ego_transform.rotation.yaw)
        rel_x = location.x - ego_loc.x
        rel_y = location.y - ego_loc.y
        local_x = np.cos(-ego_yaw) * rel_x - np.sin(-ego_yaw) * rel_y
        local_y = np.sin(-ego_yaw) * rel_x + np.cos(-ego_yaw) * rel_y
        return local_x, local_y

    @staticmethod
    def _actor_speed(actor):
        velocity = actor.get_velocity()
        return float(np.sqrt(velocity.x ** 2 + velocity.y ** 2 + velocity.z ** 2))

    def _ego_is_alive(self):
        if self.ego is None:
            return False
        try:
            return bool(self.ego.is_alive)
        except Exception:
            return False

    @staticmethod
    def _copy_obs_dict(obs):
        if obs is None:
            return None
        copied = {}
        for key, value in obs.items():
            if isinstance(value, np.ndarray):
                copied[key] = value.copy()
            else:
                copied[key] = value
        return copied

    def _make_empty_obs(self):
        obs = {}
        for key, space in self.observation_space.spaces.items():
            obs[key] = np.zeros(space.shape, dtype=space.dtype)
        if 'ego_state' in obs and obs['ego_state'].shape[0] >= 8:
            obs['ego_state'][7] = 21.0
        return obs

    def _safe_obs_snapshot(self):
        if self.last_obs is not None:
            return self._copy_obs_dict(self.last_obs)
        return self._make_empty_obs()

    def _build_step_info(
        self,
        forced_spawned_this_step=0,
        forced_completed_this_step=0,
        forced_failed_this_step=0,
        forced_timeout_this_step=0,
        lead_brake_triggered_this_step=False,
        lead_brake_completed_this_step=False,
        lead_brake_aborted_this_step=False,
        ego_destroyed=False,
        ego_destroyed_reason="",
    ):
        return {
            'is_collision': self._is_collision,
            'is_off_road': self._is_off_road,
            'forced_crossing_spawned_this_step': forced_spawned_this_step,
            'forced_crossing_spawned_total': self.current_forced_crossing_spawned,
            'forced_crossing_events': self.current_forced_crossing_events,
            'forced_crossing_active': len(self.active_forced_crossing_events),
            'forced_crossing_completed': self.current_forced_crossing_completed,
            'forced_crossing_failed': self.current_forced_crossing_failed,
            'forced_crossing_timeout': self.current_forced_crossing_timeout,
            'forced_crossing_completed_this_step': forced_completed_this_step,
            'forced_crossing_failed_this_step': forced_failed_this_step,
            'forced_crossing_timeout_this_step': forced_timeout_this_step,
            'forced_crossing_disabled': self.current_forced_crossing_disabled,
            'forced_crossing_disable_reason': self.current_forced_crossing_disable_reason,
            'lead_brake_enabled': self.current_lead_brake_enabled,
            'lead_brake_active': self.active_lead_brake_event is not None,
            'lead_brake_events': self.current_lead_brake_events,
            'lead_brake_triggered': self.current_lead_brake_triggered,
            'lead_brake_completed': self.current_lead_brake_completed,
            'lead_brake_aborted': self.current_lead_brake_aborted,
            'lead_brake_abort_reason': self.current_lead_brake_abort_reason,
            'lead_brake_last_trigger_step': self.current_lead_brake_last_trigger_step,
            'lead_brake_trigger_ego_speed': self.current_lead_brake_trigger_ego_speed,
            'lead_brake_last_state': self.current_lead_brake_last_state,
            'lead_brake_last_distance': self.current_lead_brake_last_distance,
            'lead_brake_last_relative_speed': self.current_lead_brake_last_relative_speed,
            'lead_brake_last_candidate_id': self.current_lead_brake_last_candidate_id,
            'lead_brake_triggered_this_step': lead_brake_triggered_this_step,
            'lead_brake_completed_this_step': lead_brake_completed_this_step,
            'lead_brake_aborted_this_step': lead_brake_aborted_this_step,
            'lead_brake_min_ego_speed': self.current_lead_brake_min_ego_speed,
            'hard_break_route_enabled': self.current_hard_break_route_enabled,
            'hard_break_lead_spawned': self.current_hard_break_lead_spawned,
            'hard_break_lead_vehicle_id': self.hard_break_lead_vehicle_id,
            'hard_break_lead_spawn_distance': self.current_hard_break_lead_spawn_distance,
            'hard_break_lead_spawn_failure': self.current_hard_break_lead_spawn_failure,
            'ego_destroyed': ego_destroyed,
            'ego_destroyed_reason': ego_destroyed_reason,
        }

    def _handle_destroyed_ego_step(self, reason):
        self._is_collision = True
        obs = self._safe_obs_snapshot()
        info = self._build_step_info(
            ego_destroyed=True,
            ego_destroyed_reason=reason,
        )
        info.update(self.long_tail_event_manager.build_info())
        reward = -100.0 / 50.0
        return obs, reward, True, False, info

    @staticmethod
    def _normalize_xy(vec_x, vec_y):
        norm = float(np.sqrt(vec_x ** 2 + vec_y ** 2))
        if norm < 1e-6:
            return None
        return np.array([vec_x / norm, vec_y / norm], dtype=np.float32)

    @staticmethod
    def _yaw_diff_deg(yaw_a, yaw_b):
        return abs((yaw_a - yaw_b + 180.0) % 360.0 - 180.0)

    def _advance_waypoint_along_lane(self, waypoint, distance, step_size=2.0):
        if waypoint is None:
            return None

        remaining = max(0.0, float(distance))
        current = waypoint
        previous_yaw = current.transform.rotation.yaw

        while remaining > 1e-3:
            next_distance = min(step_size, remaining)
            next_waypoints = current.next(next_distance)
            if not next_waypoints:
                return None
            current = min(
                next_waypoints,
                key=lambda wp: self._yaw_diff_deg(wp.transform.rotation.yaw, previous_yaw)
            )
            if current.is_intersection:
                return None
            previous_yaw = current.transform.rotation.yaw
            remaining -= next_distance

        return current

    def _build_forced_crossing_specs(self, count, distance_min, distance_max, lateral_min, lateral_max):
        if count <= 0 or self.ego is None:
            return []

        distance_min = max(0.0, float(distance_min))
        distance_max = max(distance_min, float(distance_max))
        lateral_min = max(0.0, float(lateral_min))
        lateral_max = max(lateral_min, float(lateral_max))

        world_map = self.world.get_map()
        ego_waypoint = world_map.get_waypoint(
            self.ego.get_location(), project_to_road=True, lane_type=carla.LaneType.Driving
        )
        if ego_waypoint is None or ego_waypoint.is_intersection:
            return []

        specs = []
        attempts = 0
        max_attempts = max(20, count * 12)

        while len(specs) < count and attempts < max_attempts:
            attempts += 1
            longitudinal_distance = random.uniform(distance_min, distance_max)
            anchor_waypoint = self._advance_waypoint_along_lane(ego_waypoint, longitudinal_distance)
            if anchor_waypoint is None or anchor_waypoint.is_intersection:
                continue

            lane_yaw = np.deg2rad(anchor_waypoint.transform.rotation.yaw)
            lane_normal = np.array([-np.sin(lane_yaw), np.cos(lane_yaw)], dtype=np.float32)

            lane_width = max(2.5, float(anchor_waypoint.lane_width))
            min_side_offset = max(lateral_min, lane_width * 1.1)
            max_side_offset = max(min_side_offset + 0.2, lateral_max)

            spawn_offset = random.uniform(min_side_offset, max_side_offset)
            target_offset = random.uniform(min_side_offset, max_side_offset)
            start_sign = -1.0 if random.random() < 0.5 else 1.0

            center = anchor_waypoint.transform.location
            spawn_location = carla.Location(
                x=float(center.x + lane_normal[0] * start_sign * spawn_offset),
                y=float(center.y + lane_normal[1] * start_sign * spawn_offset),
                z=float(center.z + 0.8),
            )
            target_location = carla.Location(
                x=float(center.x - lane_normal[0] * start_sign * target_offset),
                y=float(center.y - lane_normal[1] * start_sign * target_offset),
                z=float(center.z + 0.8),
            )

            direction = self._normalize_xy(
                target_location.x - spawn_location.x,
                target_location.y - spawn_location.y,
            )
            if direction is None:
                continue

            crossing_distance = float(np.sqrt(
                (target_location.x - spawn_location.x) ** 2 +
                (target_location.y - spawn_location.y) ** 2
            ))
            if crossing_distance < max(4.0, lane_width * 1.5):
                continue

            if any(abs(spec['anchor_distance'] - longitudinal_distance) < 2.0 for spec in specs):
                continue

            specs.append({
                'anchor_distance': longitudinal_distance,
                'spawn_location': spawn_location,
                'target_location': target_location,
                'direction': direction,
                'path_length': crossing_distance,
                'completion_margin': max(0.8, min(1.6, lane_width * 0.35)),
            })

        return specs

    @staticmethod
    def _is_timeout_error(exc):
        msg = str(exc).lower()
        return ("time-out" in msg) or ("timed out" in msg) or ("timeout" in msg)

    def _disable_forced_crossing(self, reason, exc=None):
        if not self.current_forced_crossing_disabled:
            if exc is None:
                print(f"Warning: Forced crossing disabled for this episode ({reason})")
            else:
                print(f"Warning: Forced crossing disabled for this episode ({reason}): {exc}")
        self.current_forced_crossing_disabled = True
        self.current_forced_crossing_disable_reason = reason

    def _apply_forced_crossing_control(self, event, walker=None):
        if walker is None:
            walker = self.world.get_actor(event['walker_id'])
        if walker is None:
            return False

        direction = event['direction']
        control = carla.WalkerControl()
        control.direction = carla.Vector3D(x=float(direction[0]), y=float(direction[1]), z=0.0)
        control.speed = float(event['speed'])
        control.jump = False

        try:
            walker.apply_control(control)
        except RuntimeError as e:
            print(f"Warning: Failed to apply forced crossing control to walker {event['walker_id']}: {e}")
            return False
        return True

    def _cleanup_forced_crossing_event(self, event_id, outcome, reason=None):
        event = self.active_forced_crossing_events.pop(event_id, None)
        if event is None:
            return False

        walker_id = event.get('walker_id')
        if walker_id in self.forced_crossing_walker_ids:
            self.forced_crossing_walker_ids.remove(walker_id)
        if walker_id is not None:
            self._destroy_actor_ids_batch([walker_id], label='forced crossing walker cleanup', do_tick=False)

        if outcome == 'completed':
            self.current_forced_crossing_completed += 1
        elif outcome == 'timeout':
            self.current_forced_crossing_timeout += 1
        else:
            self.current_forced_crossing_failed += 1

        if outcome != 'completed':
            suffix = f": {reason}" if reason else ""
            print(f"Info: Forced crossing event {event_id} ended with outcome={outcome}{suffix}")
        return True

    def _apply_active_forced_crossing_controls(self):
        for event_id in list(self.active_forced_crossing_events.keys()):
            event = self.active_forced_crossing_events.get(event_id)
            if event is None:
                continue

            walker = self.world.get_actor(event['walker_id'])
            if walker is None:
                self._cleanup_forced_crossing_event(event_id, 'failed', 'actor_missing_before_tick')
                continue

            if not self._apply_forced_crossing_control(event, walker=walker):
                self._cleanup_forced_crossing_event(event_id, 'failed', 'apply_control_failed')

    def _update_forced_crossing_events(self):
        completed_this_step = 0
        failed_this_step = 0
        timeout_this_step = 0

        for event_id in list(self.active_forced_crossing_events.keys()):
            event = self.active_forced_crossing_events.get(event_id)
            if event is None:
                continue

            walker = self.world.get_actor(event['walker_id'])
            if walker is None:
                if self._cleanup_forced_crossing_event(event_id, 'failed', 'actor_missing_after_tick'):
                    failed_this_step += 1
                continue

            location = walker.get_location()
            start = event['spawn_location']
            target = event['target_location']
            direction = event['direction']

            progress_x = location.x - start.x
            progress_y = location.y - start.y
            progress = float(progress_x * direction[0] + progress_y * direction[1])
            remaining_distance = float(np.sqrt(
                (target.x - location.x) ** 2 + (target.y - location.y) ** 2
            ))

            if (
                progress >= event['path_length'] - event['completion_margin']
                or remaining_distance <= event['completion_margin']
            ):
                if self._cleanup_forced_crossing_event(event_id, 'completed'):
                    completed_this_step += 1
                continue

            if self.time_step >= event['timeout_step']:
                if self._cleanup_forced_crossing_event(event_id, 'timeout', 'crossing_timeout'):
                    timeout_this_step += 1

        return completed_this_step, failed_this_step, timeout_this_step

    def _spawn_forced_crossing_walkers(
        self,
        count,
        distance_min,
        distance_max,
        lateral_min,
        lateral_max,
        tick_before_start=False,
    ):
        if count <= 0 or self.ego is None:
            return 0

        del tick_before_start

        crossing_specs = self._build_forced_crossing_specs(
            count=count,
            distance_min=distance_min,
            distance_max=distance_max,
            lateral_min=lateral_min,
            lateral_max=lateral_max,
        )
        if not crossing_specs:
            print("Info: Forced crossing walkers skipped (no valid scripted crossing geometry found)")
            return 0

        walker_bps = self.world.get_blueprint_library().filter('walker.pedestrian.*')
        started_count = 0

        for spec in crossing_specs:
            walker_bp = random.choice(walker_bps)
            if walker_bp.has_attribute('is_invincible'):
                walker_bp.set_attribute('is_invincible', 'false')

            direction = spec['direction']
            yaw = float(np.degrees(np.arctan2(direction[1], direction[0])))
            spawn_transform = carla.Transform(
                location=spec['spawn_location'],
                rotation=carla.Rotation(yaw=yaw)
            )

            try:
                walker = self.world.try_spawn_actor(walker_bp, spawn_transform)
            except RuntimeError as e:
                if self._is_timeout_error(e):
                    self._disable_forced_crossing("walker_spawn_timeout", e)
                    return started_count
                print(f"Warning: Forced crossing walker spawn failed: {e}")
                continue

            if walker is None:
                continue

            scripted_speed = random.uniform(
                self.current_walker_speed_min,
                self.current_walker_speed_max,
            ) * random.uniform(1.05, 1.35)
            timeout_steps = max(
                25,
                int(np.ceil(spec['path_length'] / max(scripted_speed, 0.1) / max(self.dt, 1e-3))) + 20
            )

            event_id = self.forced_crossing_event_seq
            self.forced_crossing_event_seq += 1
            event = {
                'event_id': event_id,
                'walker_id': walker.id,
                'spawn_location': spec['spawn_location'],
                'target_location': spec['target_location'],
                'direction': direction,
                'speed': scripted_speed,
                'path_length': spec['path_length'],
                'completion_margin': spec['completion_margin'],
                'timeout_step': self.time_step + timeout_steps,
            }
            self.active_forced_crossing_events[event_id] = event
            self.forced_crossing_walker_ids.append(walker.id)

            if not self._apply_forced_crossing_control(event, walker=walker):
                self._cleanup_forced_crossing_event(event_id, 'failed', 'initial_apply_control_failed')
                continue

            started_count += 1

        if started_count > 0:
            print(f"Forced crossing walkers spawned {started_count}/{count}, started={started_count}")
        return started_count

    def _maybe_trigger_forced_crossing(self, obs):
        if (
            self.ego is None
            or self.current_forced_crossing_walkers <= 0
            or self.current_forced_crossing_disabled
        ):
            return 0

        if self.time_step < self.current_forced_crossing_warmup_steps:
            return 0

        since_last = self.time_step - self.last_forced_crossing_step
        if since_last < self.current_forced_crossing_cooldown_steps:
            return 0

        speed = float(obs['ego_state'][3]) if obs is not None else 0.0
        if speed < self.current_forced_crossing_min_speed:
            return 0

        if random.random() >= self.current_forced_crossing_trigger_prob:
            return 0

        remaining_budget = self.current_forced_crossing_max_total - self.current_forced_crossing_spawned
        if remaining_budget <= 0:
            return 0

        trigger_cap = min(self.current_forced_crossing_walkers, remaining_budget)
        trigger_count = random.randint(1, trigger_cap)
        spawned = self._spawn_forced_crossing_walkers(
            count=trigger_count,
            distance_min=self.current_forced_crossing_distance_min,
            distance_max=self.current_forced_crossing_distance_max,
            lateral_min=self.current_forced_crossing_lateral_min,
            lateral_max=self.current_forced_crossing_lateral_max,
            tick_before_start=False,
        )
        if spawned > 0:
            self.last_forced_crossing_step = self.time_step
            self.current_forced_crossing_events += 1
            print(
                f"Info: Forced crossing triggered at step={self.time_step}, "
                f"spawned={spawned}, target={trigger_count}"
            )
        self.current_forced_crossing_spawned += spawned
        return spawned

    def _set_vehicle_autopilot(self, vehicle, enabled):
        try:
            vehicle.set_autopilot(bool(enabled))
        except RuntimeError as e:
            print(f"Warning: Failed to set autopilot={enabled} for vehicle {vehicle.id}: {e}")
            return False
        return True

    def _build_lead_brake_candidate_metrics(self, vehicle, ego_transform=None, ego_waypoint=None):
        if self.ego is None or vehicle is None:
            return None

        try:
            if not vehicle.is_alive:
                return None
        except Exception:
            return None

        if vehicle.id == self.ego.id:
            return None

        if ego_transform is None:
            ego_transform = self.ego.get_transform()

        world_map = self.world.get_map()
        if ego_waypoint is None:
            ego_waypoint = world_map.get_waypoint(
                self.ego.get_location(), project_to_road=True, lane_type=carla.LaneType.Driving
            )
        if ego_waypoint is None:
            return None

        vehicle_transform = vehicle.get_transform()
        local_x, local_y = self._world_to_ego_local(ego_transform, vehicle_transform.location)
        if (
            local_x < self.current_lead_candidate_distance_min
            or local_x > self.current_lead_candidate_distance_max
        ):
            return None

        lane_width = max(2.5, float(ego_waypoint.lane_width))
        lateral_limit = max(1.2, min(2.5, lane_width * 0.45))
        if abs(local_y) > lateral_limit:
            return None

        vehicle_speed = self._actor_speed(vehicle)
        if (
            vehicle_speed < self.current_lead_candidate_speed_min
            or vehicle_speed > self.current_lead_candidate_speed_max
        ):
            return None

        vehicle_waypoint = world_map.get_waypoint(
            vehicle_transform.location, project_to_road=True, lane_type=carla.LaneType.Driving
        )
        if vehicle_waypoint is None:
            return None

        if (
            vehicle_waypoint.road_id != ego_waypoint.road_id
            or vehicle_waypoint.lane_id != ego_waypoint.lane_id
        ):
            return None

        heading_diff = self._yaw_diff_deg(
            vehicle_transform.rotation.yaw,
            ego_transform.rotation.yaw,
        )
        if heading_diff > 25.0:
            return None

        ego_speed = self._actor_speed(self.ego)
        return {
            'vehicle_id': vehicle.id,
            'distance': float(local_x),
            'lateral_offset': float(local_y),
            'speed': vehicle_speed,
            'relative_speed': float(ego_speed - vehicle_speed),
        }

    def _find_lead_brake_candidate(self):
        if self.ego is None:
            return None

        ego_transform = self.ego.get_transform()
        ego_waypoint = self.world.get_map().get_waypoint(
            self.ego.get_location(), project_to_road=True, lane_type=carla.LaneType.Driving
        )
        if ego_waypoint is None:
            return None

        if self.current_hard_break_route_enabled and self.hard_break_lead_vehicle_id is not None:
            lead_vehicle = self.world.get_actor(self.hard_break_lead_vehicle_id)
            metrics = self._build_lead_brake_candidate_metrics(
                lead_vehicle,
                ego_transform=ego_transform,
                ego_waypoint=ego_waypoint,
            )
            if metrics is not None:
                return metrics

        best_metrics = None
        for vehicle in self.spawned_vehicles:
            metrics = self._build_lead_brake_candidate_metrics(
                vehicle,
                ego_transform=ego_transform,
                ego_waypoint=ego_waypoint,
            )
            if metrics is None:
                continue
            if best_metrics is None or metrics['distance'] < best_metrics['distance']:
                best_metrics = metrics

        return best_metrics

    def _abort_lead_brake_event(self, reason):
        event = self.active_lead_brake_event
        if event is None:
            self.current_lead_brake_abort_reason = reason
            self.current_lead_brake_last_state = "aborted"
            self.current_lead_brake_last_candidate_id = None
            return False

        self.active_lead_brake_event = None
        self.current_lead_brake_aborted += 1
        self.current_lead_brake_abort_reason = reason
        self.current_lead_brake_last_state = "aborted"
        self.current_lead_brake_last_candidate_id = event.get('vehicle_id')
        return True

    def _maybe_start_lead_brake_tracking(self):
        if (
            self.ego is None
            or not self.current_lead_brake_enabled
            or self.current_lead_brake_events >= self.current_lead_event_max_per_episode
            or self.active_lead_brake_event is not None
            or self.time_step < self.current_lead_brake_warmup_steps
        ):
            return False

        candidate = self._find_lead_brake_candidate()
        if candidate is None:
            return False

        self.active_lead_brake_event = {
            'vehicle_id': candidate['vehicle_id'],
            'state': 'tracking_candidate',
            'track_steps': 1,
            'distance': candidate['distance'],
            'relative_speed': candidate['relative_speed'],
            'brake_end_step': None,
            'trigger_step': None,
            'steer': 0.0,
        }
        self.current_lead_brake_last_state = 'tracking_candidate'
        self.current_lead_brake_last_candidate_id = candidate['vehicle_id']
        self.current_lead_brake_last_distance = candidate['distance']
        self.current_lead_brake_last_relative_speed = candidate['relative_speed']
        self.current_lead_brake_abort_reason = ""
        return True

    def _apply_active_lead_brake_control(self):
        event = self.active_lead_brake_event
        if event is None or event.get('state') != 'braking':
            return

        vehicle = self.world.get_actor(event['vehicle_id'])
        if vehicle is None:
            self._abort_lead_brake_event("lead_vehicle_missing_before_tick")
            return

        control = carla.VehicleControl(
            throttle=0.0,
            steer=float(event.get('steer', 0.0)),
            brake=float(self.current_lead_brake_strength),
        )
        try:
            vehicle.apply_control(control)
        except RuntimeError as e:
            self._abort_lead_brake_event(f"lead_brake_apply_failed:{e}")

    def _update_lead_brake_event(self):
        event = self.active_lead_brake_event
        triggered_this_step = False
        completed_this_step = False
        aborted_this_step = False

        if event is None:
            return triggered_this_step, completed_this_step, aborted_this_step

        vehicle = self.world.get_actor(event['vehicle_id'])
        if vehicle is None:
            aborted_this_step = self._abort_lead_brake_event("lead_vehicle_missing_after_tick")
            return triggered_this_step, completed_this_step, aborted_this_step

        vehicle_transform = vehicle.get_transform()
        ego_transform = self.ego.get_transform() if self.ego is not None else None
        if ego_transform is not None:
            local_x, _ = self._world_to_ego_local(ego_transform, vehicle_transform.location)
            ego_speed = self._actor_speed(self.ego)
            vehicle_speed = self._actor_speed(vehicle)
            event['distance'] = float(local_x)
            event['relative_speed'] = float(ego_speed - vehicle_speed)
            self.current_lead_brake_last_distance = event['distance']
            self.current_lead_brake_last_relative_speed = event['relative_speed']

        if event['state'] == 'tracking_candidate':
            candidate_metrics = self._build_lead_brake_candidate_metrics(vehicle)
            if candidate_metrics is None:
                aborted_this_step = self._abort_lead_brake_event("candidate_no_longer_valid")
                return triggered_this_step, completed_this_step, aborted_this_step

            event['track_steps'] += 1
            event['distance'] = candidate_metrics['distance']
            event['relative_speed'] = candidate_metrics['relative_speed']
            self.current_lead_brake_last_state = 'tracking_candidate'
            self.current_lead_brake_last_candidate_id = candidate_metrics['vehicle_id']
            self.current_lead_brake_last_distance = candidate_metrics['distance']
            self.current_lead_brake_last_relative_speed = candidate_metrics['relative_speed']

            if event['track_steps'] < self.current_lead_track_min_steps:
                return triggered_this_step, completed_this_step, aborted_this_step

            distance = candidate_metrics['distance']
            if distance > self.current_lead_trigger_distance_max:
                return triggered_this_step, completed_this_step, aborted_this_step
            if (
                not self.current_hard_break_route_enabled
                and distance < self.current_lead_trigger_distance_min
            ):
                return triggered_this_step, completed_this_step, aborted_this_step

            ego_speed = self._actor_speed(self.ego)
            if ego_speed < self.current_lead_brake_min_ego_speed:
                self.current_lead_brake_last_state = 'waiting_for_ego_speed'
                event['state'] = 'tracking_candidate'
                return triggered_this_step, completed_this_step, aborted_this_step

            current_control = vehicle.get_control()
            if not self._set_vehicle_autopilot(vehicle, False):
                aborted_this_step = self._abort_lead_brake_event("autopilot_disable_failed")
                return triggered_this_step, completed_this_step, aborted_this_step

            event['state'] = 'braking'
            event['trigger_step'] = self.time_step
            event['brake_end_step'] = self.time_step + self.current_lead_brake_hold_steps
            event['steer'] = float(current_control.steer)
            self.current_lead_brake_events += 1
            self.current_lead_brake_triggered = True
            self.current_lead_brake_last_trigger_step = self.time_step
            self.current_lead_brake_trigger_ego_speed = ego_speed
            self.current_lead_brake_last_state = 'braking'
            triggered_this_step = True
            self._apply_active_lead_brake_control()
            return triggered_this_step, completed_this_step, aborted_this_step

        if event['state'] == 'braking':
            self.current_lead_brake_last_state = 'braking'
            if self.time_step >= event['brake_end_step']:
                self._set_vehicle_autopilot(vehicle, True)
                self.active_lead_brake_event = None
                self.current_lead_brake_completed += 1
                self.current_lead_brake_last_state = 'done'
                completed_this_step = True

        return triggered_this_step, completed_this_step, aborted_this_step

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)

        # 1. 销毁传感器
        self._destroy_sensors()

        # 2. 确保同步模式
        try:
            self._set_synchronous_mode(True)
            self.world.tick()
        except Exception as e:
            print(f"Warning: Failed to enforce synchronous mode during reset: {e}")

        # 3. 重置状态变量
        self.stop_steps = 0
        self._is_collision = False
        self._is_off_road = False
        self.collision_hist = []

        # 4. 销毁上一回合长尾场景 actor（含静态物）
        self.long_tail_event_manager.destroy(do_tick=False)

        # 5. 销毁行人
        self._destroy_walkers()

        # 6. 统一销毁全部车辆（包含上一回合的 ego）
        self._clear_all_actors(['vehicle.*'])
        self.ego = None

        # 7. 等待清理完成
        self.world.tick()
        time.sleep(0.1)

        self.used_spawn_points = []
        scenario_spec = self.scenario_provider.sample(self)
        self.active_scenario = scenario_spec
        if scenario_spec.weather is not None:
            try:
                self.world.set_weather(scenario_spec.weather)
            except RuntimeError as e:
                print(f"Warning: Failed to apply scenario weather: {e}")

        npc_vehicle_count = scenario_spec.number_of_vehicles
        if npc_vehicle_count is None:
            npc_vehicle_count = self.number_of_vehicles
        npc_vehicle_count = max(0, int(npc_vehicle_count))

        npc_walker_count = scenario_spec.number_of_walkers
        if npc_walker_count is None:
            npc_walker_count = self.number_of_walkers
        npc_walker_count = max(0, int(npc_walker_count))

        pedestrian_cross_factor = scenario_spec.pedestrian_cross_factor
        if pedestrian_cross_factor is None:
            pedestrian_cross_factor = self.pedestrian_cross_factor
        self.current_pedestrian_cross_factor = max(0.0, min(1.0, float(pedestrian_cross_factor)))
        self._set_pedestrian_cross_factor(self.current_pedestrian_cross_factor)

        walker_speed_min = scenario_spec.walker_speed_min
        if walker_speed_min is None:
            walker_speed_min = self.walker_speed_min
        walker_speed_max = scenario_spec.walker_speed_max
        if walker_speed_max is None:
            walker_speed_max = self.walker_speed_max
        walker_speed_min = float(walker_speed_min)
        walker_speed_max = float(walker_speed_max)
        if walker_speed_max < walker_speed_min:
            walker_speed_max = walker_speed_min
        self.current_walker_speed_min = walker_speed_min
        self.current_walker_speed_max = walker_speed_max

        forced_crossing_walkers = scenario_spec.forced_crossing_walkers
        if forced_crossing_walkers is None:
            forced_crossing_walkers = self.forced_crossing_walkers
        forced_crossing_walkers = max(0, int(forced_crossing_walkers))

        forced_crossing_distance_min = scenario_spec.forced_crossing_distance_min
        if forced_crossing_distance_min is None:
            forced_crossing_distance_min = self.forced_crossing_distance_min
        forced_crossing_distance_max = scenario_spec.forced_crossing_distance_max
        if forced_crossing_distance_max is None:
            forced_crossing_distance_max = self.forced_crossing_distance_max
        forced_crossing_distance_min = max(0.0, float(forced_crossing_distance_min))
        forced_crossing_distance_max = float(forced_crossing_distance_max)
        if forced_crossing_distance_max < forced_crossing_distance_min:
            forced_crossing_distance_max = forced_crossing_distance_min

        forced_crossing_lateral_min = scenario_spec.forced_crossing_lateral_min
        if forced_crossing_lateral_min is None:
            forced_crossing_lateral_min = self.forced_crossing_lateral_min
        forced_crossing_lateral_max = scenario_spec.forced_crossing_lateral_max
        if forced_crossing_lateral_max is None:
            forced_crossing_lateral_max = self.forced_crossing_lateral_max
        forced_crossing_lateral_min = max(0.0, float(forced_crossing_lateral_min))
        forced_crossing_lateral_max = float(forced_crossing_lateral_max)
        if forced_crossing_lateral_max < forced_crossing_lateral_min:
            forced_crossing_lateral_max = forced_crossing_lateral_min

        forced_crossing_trigger_prob = scenario_spec.forced_crossing_trigger_prob
        if forced_crossing_trigger_prob is None:
            forced_crossing_trigger_prob = self.forced_crossing_trigger_prob
        forced_crossing_trigger_prob = max(0.0, min(1.0, float(forced_crossing_trigger_prob)))

        forced_crossing_cooldown_steps = scenario_spec.forced_crossing_cooldown_steps
        if forced_crossing_cooldown_steps is None:
            forced_crossing_cooldown_steps = self.forced_crossing_cooldown_steps
        forced_crossing_cooldown_steps = max(0, int(forced_crossing_cooldown_steps))

        forced_crossing_warmup_steps = scenario_spec.forced_crossing_warmup_steps
        if forced_crossing_warmup_steps is None:
            forced_crossing_warmup_steps = self.forced_crossing_warmup_steps
        forced_crossing_warmup_steps = max(0, int(forced_crossing_warmup_steps))

        forced_crossing_min_speed = scenario_spec.forced_crossing_min_speed
        if forced_crossing_min_speed is None:
            forced_crossing_min_speed = self.forced_crossing_min_speed
        forced_crossing_min_speed = max(0.0, float(forced_crossing_min_speed))

        forced_crossing_max_total = scenario_spec.forced_crossing_max_total
        if forced_crossing_max_total is None:
            forced_crossing_max_total = self.forced_crossing_max_total
        forced_crossing_max_total = max(0, int(forced_crossing_max_total))

        self.current_forced_crossing_walkers = forced_crossing_walkers
        self.current_forced_crossing_distance_min = forced_crossing_distance_min
        self.current_forced_crossing_distance_max = forced_crossing_distance_max
        self.current_forced_crossing_lateral_min = forced_crossing_lateral_min
        self.current_forced_crossing_lateral_max = forced_crossing_lateral_max
        self.current_forced_crossing_trigger_prob = forced_crossing_trigger_prob
        self.current_forced_crossing_cooldown_steps = forced_crossing_cooldown_steps
        self.current_forced_crossing_warmup_steps = forced_crossing_warmup_steps
        self.current_forced_crossing_min_speed = forced_crossing_min_speed
        self.current_forced_crossing_max_total = forced_crossing_max_total
        self.current_forced_crossing_spawned = 0
        self.current_forced_crossing_events = 0
        self.current_forced_crossing_completed = 0
        self.current_forced_crossing_failed = 0
        self.current_forced_crossing_timeout = 0
        self.last_forced_crossing_step = -10**9
        self.current_forced_crossing_disabled = False
        self.current_forced_crossing_disable_reason = ""
        self.current_lead_brake_enable_prob = float(
            self.lead_brake_enable_prob
            if scenario_spec.lead_brake_enable_prob is None
            else scenario_spec.lead_brake_enable_prob
        )
        self.current_lead_brake_warmup_steps = max(
            0,
            int(
                self.lead_brake_warmup_steps
                if scenario_spec.lead_brake_warmup_steps is None
                else scenario_spec.lead_brake_warmup_steps
            ),
        )
        self.current_lead_candidate_distance_min = max(
            0.0,
            float(
                self.lead_candidate_distance_min
                if scenario_spec.lead_candidate_distance_min is None
                else scenario_spec.lead_candidate_distance_min
            ),
        )
        self.current_lead_candidate_distance_max = float(
            self.lead_candidate_distance_max
            if scenario_spec.lead_candidate_distance_max is None
            else scenario_spec.lead_candidate_distance_max
        )
        if self.current_lead_candidate_distance_max < self.current_lead_candidate_distance_min:
            self.current_lead_candidate_distance_max = self.current_lead_candidate_distance_min
        self.current_lead_candidate_speed_min = max(
            0.0,
            float(
                self.lead_candidate_speed_min
                if scenario_spec.lead_candidate_speed_min is None
                else scenario_spec.lead_candidate_speed_min
            ),
        )
        self.current_lead_candidate_speed_max = float(
            self.lead_candidate_speed_max
            if scenario_spec.lead_candidate_speed_max is None
            else scenario_spec.lead_candidate_speed_max
        )
        if self.current_lead_candidate_speed_max < self.current_lead_candidate_speed_min:
            self.current_lead_candidate_speed_max = self.current_lead_candidate_speed_min
        self.current_lead_track_min_steps = max(
            1,
            int(
                self.lead_track_min_steps
                if scenario_spec.lead_track_min_steps is None
                else scenario_spec.lead_track_min_steps
            ),
        )
        self.current_lead_trigger_distance_min = max(
            0.0,
            float(
                self.lead_trigger_distance_min
                if scenario_spec.lead_trigger_distance_min is None
                else scenario_spec.lead_trigger_distance_min
            ),
        )
        self.current_lead_trigger_distance_max = float(
            self.lead_trigger_distance_max
            if scenario_spec.lead_trigger_distance_max is None
            else scenario_spec.lead_trigger_distance_max
        )
        if self.current_lead_trigger_distance_max < self.current_lead_trigger_distance_min:
            self.current_lead_trigger_distance_max = self.current_lead_trigger_distance_min
        self.current_lead_brake_strength = max(
            0.0,
            min(
                1.0,
                float(
                    self.lead_brake_strength
                    if scenario_spec.lead_brake_strength is None
                    else scenario_spec.lead_brake_strength
                ),
            ),
        )
        self.current_lead_brake_hold_steps = max(
            1,
            int(
                self.lead_brake_hold_steps
                if scenario_spec.lead_brake_hold_steps is None
                else scenario_spec.lead_brake_hold_steps
            ),
        )
        self.current_lead_brake_min_ego_speed = max(
            0.0,
            float(
                self.lead_brake_min_ego_speed
                if scenario_spec.lead_brake_min_ego_speed is None
                else scenario_spec.lead_brake_min_ego_speed
            ),
        )
        self.current_lead_event_max_per_episode = max(
            0,
            int(
                self.lead_event_max_per_episode
                if scenario_spec.lead_event_max_per_episode is None
                else scenario_spec.lead_event_max_per_episode
            ),
        )
        self.current_hard_break_route_enabled = bool(
            self.hard_break_route_enabled
            if scenario_spec.hard_break_route_enabled is None
            else scenario_spec.hard_break_route_enabled
        )
        self.current_hard_break_lead_spawn_distance_min = max(
            0.0,
            float(
                self.hard_break_lead_spawn_distance_min
                if scenario_spec.hard_break_lead_spawn_distance_min is None
                else scenario_spec.hard_break_lead_spawn_distance_min
            ),
        )
        self.current_hard_break_lead_spawn_distance_max = float(
            self.hard_break_lead_spawn_distance_max
            if scenario_spec.hard_break_lead_spawn_distance_max is None
            else scenario_spec.hard_break_lead_spawn_distance_max
        )
        if (
            self.current_hard_break_lead_spawn_distance_max
            < self.current_hard_break_lead_spawn_distance_min
        ):
            self.current_hard_break_lead_spawn_distance_max = (
                self.current_hard_break_lead_spawn_distance_min
            )
        self.current_hard_break_lead_spawned = False
        self.current_hard_break_lead_spawn_failure = ""
        self.current_hard_break_lead_spawn_distance = None
        self.hard_break_lead_vehicle_id = None
        self.current_lead_brake_enabled = (
            self.current_lead_event_max_per_episode > 0
            and (
                self.current_hard_break_route_enabled
                or random.random() < max(0.0, min(1.0, self.current_lead_brake_enable_prob))
            )
        )
        self.current_lead_brake_events = 0
        self.current_lead_brake_triggered = False
        self.current_lead_brake_completed = 0
        self.current_lead_brake_aborted = 0
        self.current_lead_brake_abort_reason = ""
        self.current_lead_brake_last_trigger_step = -1
        self.current_lead_brake_trigger_ego_speed = None
        self.current_lead_brake_last_state = "idle"
        self.current_lead_brake_last_distance = None
        self.current_lead_brake_last_relative_speed = None
        self.current_lead_brake_last_candidate_id = None
        self.active_lead_brake_event = None

        # ============ 生成 NPC ============
        self.spawned_vehicles, self.used_spawn_points = self._spawn_vehicles_batch(npc_vehicle_count)
        self.world.tick()

        self._spawn_walkers_batch(npc_walker_count)
        self.world.tick()

        # Get actors' polygon list
        self.vehicle_polygons = []
        vehicle_poly_dict = self._get_actor_polygons('vehicle.*')
        self.vehicle_polygons.append(vehicle_poly_dict)

        self.walker_polygons = []
        walker_poly_dict = self._get_actor_polygons('walker.*')
        self.walker_polygons.append(walker_poly_dict)

        # Spawn the ego vehicle
        ego_spawn_times = 0
        preferred_ego_spawn = scenario_spec.ego_spawn_point
        preferred_ego_spawn_consumed = preferred_ego_spawn is None
        while True:
            if ego_spawn_times > self.max_ego_spawn_times:
                raise RuntimeError(
                    "Reset Failed: Could not spawn ego vehicle after multiple attempts.")

            if not preferred_ego_spawn_consumed:
                transform = preferred_ego_spawn
                preferred_ego_spawn_consumed = True
            else:
                available_spawn_points = [
                    sp for sp in self.vehicle_spawn_points if sp not in self.used_spawn_points
                ]

                if len(available_spawn_points) > 0:
                    transform = random.choice(available_spawn_points)
                else:
                    transform = random.choice(self.vehicle_spawn_points)

            if self._try_spawn_ego_vehicle_at(transform):
                break
            else:
                ego_spawn_times += 1

        # 关键：spawn 后立即 tick，确保位置信息正确
        self.world.tick()

        min_route_distance = scenario_spec.min_route_distance
        if min_route_distance is None:
            min_route_distance = 50.0

        self.global_route, route_reason = self._plan_global_route(
            preferred_destination=scenario_spec.destination_transform,
            min_route_distance=min_route_distance
        )

        if route_reason == "no_route":
            print("Warning: Could not build a valid route; using current location as target")
        elif route_reason != "forward":
            print(f"Info: Route planner fallback used ({route_reason})")

        if len(self.global_route) > 0:
            self.target_destination = self.global_route[-1].location
        else:
            self.target_destination = self.ego.get_location()

        if self.current_hard_break_route_enabled:
            self._spawn_hard_break_lead_vehicle()
            self.world.tick()

        self.long_tail_event_manager.setup(scenario_spec.long_tail_events)
        if scenario_spec.long_tail_events:
            self.world.tick()

        # self.prev_dist_to_goal = self.ego.get_location().distance(self.target_destination)
        self.last_closest_idx = 0
        self.last_rewarded_idx = 0

        traffic_policy = scenario_spec.traffic_light_policy or self.traffic
        if traffic_policy == 'off':
            for actor in self.world.get_actors().filter('traffic.traffic_light*'):
                actor.set_state(carla.TrafficLightState.Green)
                actor.freeze(True)
        elif traffic_policy == 'on':
            for actor in self.world.get_actors().filter('traffic.traffic_light*'):
                actor.freeze(False)

        # ============ 创建传感器 ============
        self.collision_sensor = self.world.spawn_actor(
            self.collision_bp, carla.Transform(), attach_to=self.ego
        )
        self.collision_sensor.listen(self._on_collision)

        if self.enable_lidar:
            self.lidar_sensor = self.world.spawn_actor(
                self.lidar_bp, self.lidar_trans, attach_to=self.ego
            )
            self.lidar_sensor.listen(self._on_lidar)

        if self.enable_camera:
            self.camera_sensor = self.world.spawn_actor(
                self.camera_bp, self.camera_trans, attach_to=self.ego
            )
            self.camera_sensor.listen(self._on_camera)

        if self.enable_bev:
            self.bev_semantic_sensor = self.world.spawn_actor(
                self.bev_semantic_bp, self.bev_semantic_trans, attach_to=self.ego
            )
            self.bev_semantic_sensor.listen(self._on_bev_semantic)

        # Update timesteps
        self.time_step = 1
        self.reset_step += 1

        self._set_synchronous_mode(True)

        # 等待传感器数据
        for _ in range(5):
            self.world.tick()

            # 检查是否所有开启的传感器都有数据了
            ready = True
            if self.enable_lidar and self.lidar_data is None:
                ready = False
            if self.enable_camera and self.camera_data is None:
                ready = False
            if self.enable_bev and self.bev_semantic_image is None:
                ready = False

            if ready:
                break

        # ✅ 关键修复：在所有 tick 完成后，车辆稳定后再计算初始距离！
        self.prev_dist_to_goal = self.ego.get_location().distance(self.target_destination)

        # ✅ 同时更新 last_closest_idx，确保与当前车辆位置一致
        # 调用一次 _get_obs 来同步 last_closest_idx
        obs = self._get_obs()
        # 然后让 last_rewarded_idx 与 last_closest_idx 同步，避免第一步就给大量路点奖励
        self.last_rewarded_idx = self.last_closest_idx

        active_backend = scenario_spec.metadata.get('backend', self.scenario_backend)
        info = {
            'scenario_backend': active_backend,
            'scenario_backend_configured': self.scenario_backend,
            'scenario_metadata': scenario_spec.metadata,
            'route_plan_reason': route_reason,
            'pedestrian_cross_factor': self.current_pedestrian_cross_factor,
            'walker_speed_min': self.current_walker_speed_min,
            'walker_speed_max': self.current_walker_speed_max,
            'forced_crossing_walkers': self.current_forced_crossing_walkers,
            'forced_crossing_distance_min': self.current_forced_crossing_distance_min,
            'forced_crossing_distance_max': self.current_forced_crossing_distance_max,
            'forced_crossing_lateral_min': self.current_forced_crossing_lateral_min,
            'forced_crossing_lateral_max': self.current_forced_crossing_lateral_max,
            'forced_crossing_trigger_prob': self.current_forced_crossing_trigger_prob,
            'forced_crossing_cooldown_steps': self.current_forced_crossing_cooldown_steps,
            'forced_crossing_warmup_steps': self.current_forced_crossing_warmup_steps,
            'forced_crossing_min_speed': self.current_forced_crossing_min_speed,
            'forced_crossing_max_total': self.current_forced_crossing_max_total,
            'forced_crossing_spawned': self.current_forced_crossing_spawned,
            'forced_crossing_events': self.current_forced_crossing_events,
            'forced_crossing_active': len(self.active_forced_crossing_events),
            'forced_crossing_completed': self.current_forced_crossing_completed,
            'forced_crossing_failed': self.current_forced_crossing_failed,
            'forced_crossing_timeout': self.current_forced_crossing_timeout,
            'forced_crossing_disabled': self.current_forced_crossing_disabled,
            'forced_crossing_disable_reason': self.current_forced_crossing_disable_reason,
            'lead_brake_enabled': self.current_lead_brake_enabled,
            'lead_brake_enable_prob': self.current_lead_brake_enable_prob,
            'lead_brake_warmup_steps': self.current_lead_brake_warmup_steps,
            'lead_candidate_distance_min': self.current_lead_candidate_distance_min,
            'lead_candidate_distance_max': self.current_lead_candidate_distance_max,
            'lead_candidate_speed_min': self.current_lead_candidate_speed_min,
            'lead_candidate_speed_max': self.current_lead_candidate_speed_max,
            'lead_track_min_steps': self.current_lead_track_min_steps,
            'lead_trigger_distance_min': self.current_lead_trigger_distance_min,
            'lead_trigger_distance_max': self.current_lead_trigger_distance_max,
            'lead_brake_strength': self.current_lead_brake_strength,
            'lead_brake_hold_steps': self.current_lead_brake_hold_steps,
            'lead_brake_min_ego_speed': self.current_lead_brake_min_ego_speed,
            'lead_event_max_per_episode': self.current_lead_event_max_per_episode,
            'lead_brake_events': self.current_lead_brake_events,
            'lead_brake_triggered': self.current_lead_brake_triggered,
            'lead_brake_completed': self.current_lead_brake_completed,
            'lead_brake_aborted': self.current_lead_brake_aborted,
            'lead_brake_abort_reason': self.current_lead_brake_abort_reason,
            'lead_brake_last_trigger_step': self.current_lead_brake_last_trigger_step,
            'lead_brake_trigger_ego_speed': self.current_lead_brake_trigger_ego_speed,
            'lead_brake_last_state': self.current_lead_brake_last_state,
            'lead_brake_last_distance': self.current_lead_brake_last_distance,
            'lead_brake_last_relative_speed': self.current_lead_brake_last_relative_speed,
            'lead_brake_last_candidate_id': self.current_lead_brake_last_candidate_id,
            'lead_brake_active': self.active_lead_brake_event is not None,
            'hard_break_route_enabled': self.current_hard_break_route_enabled,
            'hard_break_lead_spawned': self.current_hard_break_lead_spawned,
            'hard_break_lead_vehicle_id': self.hard_break_lead_vehicle_id,
            'hard_break_lead_spawn_distance_min': self.current_hard_break_lead_spawn_distance_min,
            'hard_break_lead_spawn_distance_max': self.current_hard_break_lead_spawn_distance_max,
            'hard_break_lead_spawn_distance': self.current_hard_break_lead_spawn_distance,
            'hard_break_lead_spawn_failure': self.current_hard_break_lead_spawn_failure,
        }
        info.update(self.long_tail_event_manager.build_info())
        return obs, info

    def _on_collision(self, event):
        """碰撞传感器回调"""
        impulse = event.normal_impulse
        intensity = np.sqrt(impulse.x ** 2 + impulse.y ** 2 + impulse.z ** 2)
        self.collision_hist.append(intensity)
        if len(self.collision_hist) > self.collision_hist_l:
            self.collision_hist.pop(0)

    def _on_lidar(self, data):
        """LiDAR 传感器回调"""
        self.lidar_data = data

    def _on_camera(self, data):
        """RGB 摄像头回调"""
        self.camera_data = data

    def _on_bev_semantic(self, data):
        """
        BEV 语义分割摄像头回调
        [修复] 在回调中立即转换并保存为 numpy 数组，避免重复 convert() 导致的闪烁
        """
        # 1. 先转换为 CityScapes 调色板（只在原始数据上执行一次）
        data.convert(carla.ColorConverter.CityScapesPalette)

        # 2. 立即转换为 numpy 数组
        bev_raw = np.array(data.raw_data)
        bev_bgra = bev_raw.reshape((self.bev_image_size_y, self.bev_image_size_x, 4))

        # 3. BGR -> RGB，去除 Alpha 通道，.copy() 确保数据独立
        bev_rgb = bev_bgra[:, :, :3][:, :, ::-1].copy()

        # 4. 保存处理后的图像
        self.bev_semantic_image = bev_rgb

    def step(self, action):
        if not self._ego_is_alive():
            return self._handle_destroyed_ego_step("ego_invalid_before_step")

        if not self.ego_autopilot:
            steer = float(np.clip(action[0], -1.0, 1.0))
            acceleration = float(np.clip(action[1], -1.0, 1.0))

            if acceleration >= 0:
                throttle = acceleration
                brake = 0.0
            else:
                throttle = 0.0
                brake = -acceleration

            control = carla.VehicleControl(throttle=throttle, steer=steer, brake=brake)
            try:
                self.ego.apply_control(control)
            except RuntimeError as e:
                if not self._ego_is_alive():
                    return self._handle_destroyed_ego_step(f"ego_apply_control_failed:{e}")
                raise
        self._apply_active_forced_crossing_controls()
        self._apply_active_lead_brake_control()
        self.long_tail_event_manager.apply_before_tick()

        self.world.tick()

        if not self._ego_is_alive():
            return self._handle_destroyed_ego_step("ego_invalid_after_tick")

        # Set spectator view
        spectator = self.world.get_spectator()
        try:
            transform = self.ego.get_transform()
        except RuntimeError as e:
            if not self._ego_is_alive():
                return self._handle_destroyed_ego_step(f"ego_transform_failed:{e}")
            raise
        if self.view_mode == 'top':
            spectator.set_transform(
                carla.Transform(
                    transform.location + carla.Location(z=40),
                    carla.Rotation(pitch=-90)
                )
            )
        elif self.view_mode == 'follow':
            cam_location = transform.transform(carla.Location(x=-6.0, z=3.0))
            cam_rotation = carla.Rotation(pitch=-10, yaw=transform.rotation.yaw, roll=0)
            spectator.set_transform(carla.Transform(cam_location, cam_rotation))

        self.time_step += 1
        self.total_step += 1

        (
            forced_completed_this_step,
            forced_failed_this_step,
            forced_timeout_this_step,
        ) = self._update_forced_crossing_events()
        (
            lead_brake_triggered_this_step,
            lead_brake_completed_this_step,
            lead_brake_aborted_this_step,
        ) = self._update_lead_brake_event()
        self.long_tail_event_manager.update_after_tick()

        try:
            obs = self._get_obs()
        except RuntimeError as e:
            if not self._ego_is_alive():
                return self._handle_destroyed_ego_step(f"ego_obs_failed:{e}")
            raise
        self.last_obs = self._copy_obs_dict(obs)

        # 先更新红灯状态（供 reward 使用）
        self.is_red_light_present = False
        try:
            if self.ego.is_at_traffic_light():
                traffic_light = self.ego.get_traffic_light()
                if traffic_light and traffic_light.get_state() == carla.TrafficLightState.Red:
                    self.is_red_light_present = True
        except RuntimeError as e:
            if not self._ego_is_alive():
                return self._handle_destroyed_ego_step(f"ego_traffic_light_failed:{e}")
            raise

        try:
            terminated = self._terminal(obs)
        except RuntimeError as e:
            if not self._ego_is_alive():
                return self._handle_destroyed_ego_step(f"ego_terminal_failed:{e}")
            raise
        truncated = False

        if self.time_step > self.max_time_episode:
            terminated = False
            truncated = True

        try:
            reward = self._get_reward(obs, terminated or truncated)
        except RuntimeError as e:
            if not self._ego_is_alive():
                return self._handle_destroyed_ego_step(f"ego_reward_failed:{e}")
            raise

        # 停车检测逻辑
        speed = obs['ego_state'][3]
        front_dist = obs['ego_state'][7]

        is_blocked_by_vehicle = (0 < front_dist < 10.0)
        is_justified_stop = self.is_red_light_present or is_blocked_by_vehicle

        # if speed < 0.1:
        #     if is_justified_stop:
        #         self.stop_steps = 0
        #     else:
        #         self.stop_steps += 1
        # else:
        #     self.stop_steps = 0
        #
        # if self.stop_steps > 100:
        #     truncated = True
        #     reward -= 5.0
        #     print("stop time out")

        forced_spawned_this_step = 0
        if not (terminated or truncated):
            try:
                forced_spawned_this_step = self._maybe_trigger_forced_crossing(obs)
            except Exception as e:
                self._disable_forced_crossing("trigger_exception", e)
            try:
                self._maybe_start_lead_brake_tracking()
            except Exception as e:
                self._abort_lead_brake_event(f"lead_brake_tracking_exception:{e}")

        info = self._build_step_info(
            forced_spawned_this_step=forced_spawned_this_step,
            forced_completed_this_step=forced_completed_this_step,
            forced_failed_this_step=forced_failed_this_step,
            forced_timeout_this_step=forced_timeout_this_step,
            lead_brake_triggered_this_step=lead_brake_triggered_this_step,
            lead_brake_completed_this_step=lead_brake_completed_this_step,
            lead_brake_aborted_this_step=lead_brake_aborted_this_step,
        )
        info.update(self.long_tail_event_manager.build_info())

        return obs, reward, terminated, truncated, info

    def _create_vehicle_bluepprint(self, actor_filter, color=None, number_of_wheels=[4]):
        blueprints = self.world.get_blueprint_library().filter(actor_filter)
        blueprint_library = []

        for nw in number_of_wheels:
            blueprint_library += [x for x in blueprints if int(x.get_attribute('number_of_wheels')) == nw]

        bp = random.choice(blueprint_library)

        if bp.has_attribute('color'):
            if not color:
                color = random.choice(bp.get_attribute('color').recommended_values)
            bp.set_attribute('color', color)

        return bp

    def _set_synchronous_mode(self, synchronous=True):
        self.settings.synchronous_mode = synchronous
        max_retries = 5
        for i in range(max_retries):
            try:
                self.world.apply_settings(self.settings)
                return
            except RuntimeError as e:
                print(f"Warning: apply_settings failed (attempt {i + 1}/{max_retries}): {e}")
                time.sleep(0.5)
        print("Error: Failed to apply synchronous mode settings after retries.")

    def _set_pedestrian_cross_factor(self, cross_factor):
        value = max(0.0, min(1.0, float(cross_factor)))
        try:
            self.world.set_pedestrians_cross_factor(value)
        except Exception as e:
            print(f"Warning: Failed to set pedestrians cross factor to {value:.2f}: {e}")

    def _try_spawn_ego_vehicle_at(self, transform):
        vehicle = None
        overlap = False

        for idx, poly in self.vehicle_polygons[-1].items():
            poly_center = np.mean(poly, axis=0)
            ego_center = np.array([transform.location.x, transform.location.y])
            dis = np.linalg.norm(poly_center - ego_center)

            if dis > 8:
                continue
            else:
                overlap = True
                break

        if not overlap:
            vehicle = self.world.try_spawn_actor(self.ego_bp, transform)

        if vehicle is not None:
            self.ego = vehicle
            return True
        return False

    def _route_is_forward(self, route):
        if not route or self.ego is None:
            return False

        ego_transform = self.ego.get_transform()
        ego_yaw = np.deg2rad(ego_transform.rotation.yaw)
        ego_loc = ego_transform.location

        check_idx = min(3, len(route) - 1)
        wp_loc = route[check_idx].location
        rel_x = wp_loc.x - ego_loc.x
        rel_y = wp_loc.y - ego_loc.y
        local_x = np.cos(-ego_yaw) * rel_x - np.sin(-ego_yaw) * rel_y
        return local_x > 0.0

    def _build_route_candidates(self, preferred_destination=None):
        candidates = []
        seen = set()

        def add_candidate(transform):
            if transform is None:
                return
            loc = transform.location
            key = (round(loc.x, 2), round(loc.y, 2), round(loc.z, 2))
            if key in seen:
                return
            seen.add(key)
            candidates.append(transform)

        add_candidate(preferred_destination)

        if self.ego is None:
            return candidates

        ego_location = self.ego.get_location()
        spawn_points = list(self.vehicle_spawn_points)
        random.shuffle(spawn_points)
        spawn_points.sort(key=lambda sp: ego_location.distance(sp.location), reverse=True)
        for transform in spawn_points:
            add_candidate(transform)
        return candidates

    def _plan_global_route(self, preferred_destination=None, min_route_distance=50.0):
        if self.ego is None:
            return [], "no_ego"

        min_route_distance = max(0.0, float(min_route_distance))
        candidates = self._build_route_candidates(preferred_destination=preferred_destination)
        ego_location = self.ego.get_location()

        best_non_forward = []
        best_non_forward_len = 0
        best_any = []
        best_any_len = 0

        for destination_transform in candidates:
            dist = ego_location.distance(destination_transform.location)
            raw_route = self.route_planner.trace_route(ego_location, destination_transform.location)
            if len(raw_route) < 2:
                continue

            route = [waypoint.transform for waypoint, _ in raw_route]
            route_len = len(route)
            if route_len > best_any_len:
                best_any = route
                best_any_len = route_len

            if dist < min_route_distance or route_len < 10:
                continue

            if self._route_is_forward(route):
                return route, "forward"

            if route_len > best_non_forward_len:
                best_non_forward = route
                best_non_forward_len = route_len

        if best_non_forward_len > 0:
            return best_non_forward, "fallback_non_forward"
        if best_any_len >= 10:
            return best_any, "fallback_distance_relaxed"
        if best_any_len > 0:
            return best_any, "fallback_short_route"
        return [], "no_route"

    def _get_actor_polygons(self, filt):
        actor_poly_dict = {}
        for actor in self.world.get_actors().filter(filt):
            trans = actor.get_transform()
            x = trans.location.x
            y = trans.location.y
            yaw = trans.rotation.yaw / 180 * np.pi
            bb = actor.bounding_box
            l = bb.extent.x
            w = bb.extent.y
            poly_local = np.array([[l, w], [l, -w], [-l, -w], [-l, w]]).transpose()
            R = np.array([[np.cos(yaw), -np.sin(yaw)], [np.sin(yaw), np.cos(yaw)]])
            poly = np.matmul(R, poly_local).transpose() + np.repeat([[x, y]], 4, axis=0)
            actor_poly_dict[actor.id] = poly
        return actor_poly_dict

    def _get_obs(self):
        obs = {}

        # ========================== LIDAR ==========================
        max_range = self.lidar_max_range
        lidar_features = np.full((240,), max_range, dtype=np.float32)

        ego_transform = self.ego.get_transform()
        ego_x = ego_transform.location.x
        ego_y = ego_transform.location.y
        ego_yaw = np.deg2rad(ego_transform.rotation.yaw)

        if self.enable_lidar and self.lidar_data is not None:
            for detection in self.lidar_data:
                x = detection.point.x
                y = detection.point.y
                local_x = np.cos(-ego_yaw) * x - np.sin(-ego_yaw) * y
                local_y = np.sin(-ego_yaw) * x + np.cos(-ego_yaw) * y
                distance = np.sqrt(local_x ** 2 + local_y ** 2)
                angle = np.arctan2(local_y, local_x)
                angle_deg = (np.degrees(angle) + 360) % 360
                index = int(angle_deg // 1.5)
                if index < 240:
                    lidar_features[index] = min(lidar_features[index], distance)
        else:
            lidar_features[:] = 0.0

        lidar_features /= max_range
        obs['lidar'] = lidar_features

        # ========================== Ego State ==========================
        velocity = self.ego.get_velocity()
        speed = np.sqrt(velocity.x ** 2 + velocity.y ** 2 + velocity.z ** 2)
        angular_velocity = self.ego.get_angular_velocity()
        acceleration = self.ego.get_acceleration()

        front_vehicle_distance = 21.0  # 初始值改为21.0，代表视野内无车
        relative_speed = 0.0
        min_front_distance = 20.0
        ego_extent = self.ego.bounding_box.extent.x

        vehicle_list = self.world.get_actors().filter('vehicle.*')

        for vehicle in vehicle_list:
            if vehicle.id == self.ego.id:
                continue
            transform = vehicle.get_transform()
            rel_x = transform.location.x - ego_x
            rel_y = transform.location.y - ego_y
            local_x = np.cos(-ego_yaw) * rel_x - np.sin(-ego_yaw) * rel_y
            local_y = np.sin(-ego_yaw) * rel_x + np.cos(-ego_yaw) * rel_y

            if 0 < local_x < min_front_distance and abs(local_y) < 2.5:
                d_center = np.sqrt(local_x ** 2 + local_y ** 2)

                # [修改] 先计算当前这辆车的净空距离 (Gap)
                current_gap = max(0.0, d_center - (ego_extent * 2))

                # [修改] 逻辑修复：拿 Gap 和 Gap 比较，而不是拿中心距离和 Gap 比较
                if current_gap < front_vehicle_distance:
                    front_vehicle_distance = current_gap  # [修改] 更新最小净空距离
                    front_speed = vehicle.get_velocity()
                    front_speed_mag = np.sqrt(front_speed.x ** 2 + front_speed.y ** 2 + front_speed.z ** 2)
                    relative_speed = speed - front_speed_mag

        ego_state = np.array([
            ego_x, ego_y, ego_yaw, speed, angular_velocity.z,
            acceleration.x, acceleration.y, front_vehicle_distance, relative_speed
        ], dtype=np.float32)
        obs['ego_state'] = ego_state

        # ========================== Nearby Vehicles ==========================
        max_vehicles = self.max_nearby_vehicles
        perception_range = self.lidar_max_range
        vehicle_data = []

        for vehicle in vehicle_list:
            if vehicle.id == self.ego.id:
                continue
            transform = vehicle.get_transform()
            x = transform.location.x
            y = transform.location.y
            yaw = np.deg2rad(transform.rotation.yaw)
            rel_x = x - ego_x
            rel_y = y - ego_y
            distance = np.sqrt(rel_x ** 2 + rel_y ** 2)

            if distance > perception_range:
                continue

            local_x = np.cos(-ego_yaw) * rel_x - np.sin(-ego_yaw) * rel_y
            local_y = np.sin(-ego_yaw) * rel_x + np.cos(-ego_yaw) * rel_y
            v = vehicle.get_velocity()
            spd = np.sqrt(v.x ** 2 + v.y ** 2 + v.z ** 2)
            vehicle_data.append((distance, [local_x, local_y, yaw - ego_yaw, spd]))

        vehicle_data.sort(key=lambda x: x[0])
        nearby_vehicles = [data for _, data in vehicle_data[:max_vehicles]]

        while len(nearby_vehicles) < max_vehicles:
            nearby_vehicles.append([0.0, 0.0, 0.0, 0.0])
        obs['nearby_vehicles'] = np.array(nearby_vehicles, dtype=np.float32).flatten()

        # ========================== Waypoints ==========================
        max_waypoints = self.max_waypoints
        world_map = self.world.get_map()
        waypoint = world_map.get_waypoint(self.ego.get_location())
        waypoints_array = np.zeros((max_waypoints, 3), dtype=np.float32)

        for i in range(max_waypoints):
            if waypoint is None:
                break
            loc = waypoint.transform.location
            yaw = waypoint.transform.rotation.yaw
            local_x = np.cos(-ego_yaw) * (loc.x - ego_x) - np.sin(-ego_yaw) * (loc.y - ego_y)
            local_y = np.sin(-ego_yaw) * (loc.x - ego_x) + np.cos(-ego_yaw) * (loc.y - ego_y)
            yaw_relative = np.deg2rad(yaw) - ego_yaw
            waypoints_array[i] = [local_x, local_y, yaw_relative]
            next_waypoints = waypoint.next(2.0)
            waypoint = next_waypoints[0] if next_waypoints else None

        obs['waypoints'] = waypoints_array.flatten()

        # ========================== Lane Info ==========================
        waypoint_center = world_map.get_waypoint(
            self.ego.get_location(), project_to_road=True, lane_type=carla.LaneType.Driving
        )

        if waypoint_center is None:
            obs['lane_info'] = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        else:
            lane_width = waypoint_center.lane_width
            ego_location = self.ego.get_location()
            center_location = waypoint_center.transform.location
            lateral_offset = np.linalg.norm(np.array([
                ego_location.x - center_location.x,
                ego_location.y - center_location.y
            ]))
            # --- [新增] 计算角度差 (Road Yaw - Ego Yaw) ---
            lane_yaw = np.deg2rad(waypoint_center.transform.rotation.yaw)
            # ego_yaw 在函数开头已经获取过了，是弧度制
            # 注意：ego_yaw = np.deg2rad(ego_transform.rotation.yaw)

            angle_diff = ego_yaw - lane_yaw
            # 归一化到 [-pi, pi]
            angle_diff = (angle_diff + np.pi) % (2 * np.pi) - np.pi

            # [修改] 返回 [车道宽, 偏离值, 角度差]
            obs['lane_info'] = np.array([lane_width, lateral_offset, angle_diff], dtype=np.float32)

        # ========================== Camera ==========================
        if self.enable_camera and self.camera_data is not None:
            raw_data = np.array(self.camera_data.raw_data)
            image_bgra = raw_data.reshape((self.image_size_y, self.image_size_x, 4))
            image_rgb = image_bgra[:, :, :3][:, :, ::-1]
            image = image_rgb
        else:
            # 如果没开摄像头，返回全黑图像以保持维度一致
            image = np.zeros((self.image_size_y, self.image_size_x, 3), dtype=np.uint8)

        obs['camera'] = image

        # ========================== BEV Semantic ==========================
        if self.enable_bev and self.bev_semantic_image is not None:
            bev_image = self.bev_semantic_image
        else:
            # 没开则返回全黑
            bev_image = np.zeros((self.bev_image_size_y, self.bev_image_size_x, 3), dtype=np.uint8)

        obs['bev_semantic'] = bev_image

        # ========================== GPS Targets ==========================
        gps_targets_array = np.zeros((self.num_lookahead, 2), dtype=np.float32)

        if self.global_route is not None and len(self.global_route) > 0:
            ego_location = ego_transform.location

            search_start = self.last_closest_idx
            search_len = 20
            search_end = min(search_start + search_len, len(self.global_route))

            min_dist = float('inf')
            closest_local_idx = -1
            current_subset = self.global_route[search_start:search_end]

            for i, transform in enumerate(current_subset):
                dist = ego_location.distance(transform.location)
                if dist < min_dist:
                    min_dist = dist
                    closest_local_idx = i

            if closest_local_idx != -1:
                current_closest_idx = search_start + closest_local_idx
            else:
                current_closest_idx = self.last_closest_idx

            closest_wp = self.global_route[current_closest_idx]
            rel_x_check = closest_wp.location.x - ego_location.x
            rel_y_check = closest_wp.location.y - ego_location.y
            local_x_check = np.cos(-ego_yaw) * rel_x_check - np.sin(-ego_yaw) * rel_y_check

            if local_x_check < 0:
                target_start_idx = current_closest_idx + 1
            else:
                target_start_idx = current_closest_idx

            self.last_closest_idx = max(self.last_closest_idx, current_closest_idx)

            targets_global = []
            for i in range(self.num_lookahead):
                target_idx = target_start_idx + i
                if target_idx >= len(self.global_route):
                    target_idx = len(self.global_route) - 1
                targets_global.append(self.global_route[target_idx].location)

            for i, loc_global in enumerate(targets_global):
                rel_x = loc_global.x - ego_location.x
                rel_y = loc_global.y - ego_location.y
                local_x = np.cos(-ego_yaw) * rel_x - np.sin(-ego_yaw) * rel_y
                local_y = np.sin(-ego_yaw) * rel_x + np.cos(-ego_yaw) * rel_y
                gps_targets_array[i, :] = [local_x, local_y]

        obs['gps_targets'] = gps_targets_array

        # ========================== Visualize Waypoints ==========================
        if self.visualize_waypoints:
            for i in range(max_waypoints):
                wx, wy, _ = waypoints_array[i]
                gx = np.cos(ego_yaw) * wx - np.sin(ego_yaw) * wy + ego_x
                gy = np.sin(ego_yaw) * wx + np.cos(ego_yaw) * wy + ego_y
                self.world.debug.draw_point(
                    carla.Location(x=gx, y=gy, z=ego_transform.location.z + 1.0),
                    size=0.1, life_time=0.5, color=carla.Color(0, 255, 0)
                )

        return obs

    def _get_reward(self, obs, done):
        reward = 0.0
        ego_state = obs['ego_state']
        speed = ego_state[3]
        acc_x = ego_state[5]
        acc_y = ego_state[6]
        front_dist = ego_state[7]
        lane_width, lateral_offset, angle_diff = obs['lane_info']

        # --- 核心约束逻辑 ---
        # 1. 角度约束：abs(angle_diff) < 0.25 (约30度)
        is_heading_aligned = abs(angle_diff) < 0.1

        # 2. 位置约束：lateral_offset < 1/3 车道宽
        is_centered = lateral_offset < (lane_width / 5.0)

        # 只有“姿态端正”才算合法的停车等待
        is_good_pose = is_heading_aligned and is_centered

        target_speed = self.desired_speed
        should_stop = False

        if 0 <= front_dist < 20.0:
            safe_speed = max(0, (front_dist - 2.0) * 0.5)
            target_speed = min(self.desired_speed, safe_speed)
            if front_dist < 2.0:
                should_stop = True

        if self.is_red_light_present:
            target_speed = 0.0
            should_stop = True

        if speed <= target_speed:
            reward += 1.0 * speed
        else:
            reward += -2.0 * (speed - target_speed)

        if target_speed > 1.0 and speed < 0.1:
            reward += -5.0

        reward += -1.0 * lateral_offset
        reward += -1.0 * abs(acc_y)

        if target_speed < self.desired_speed and speed <= target_speed and is_good_pose:
            reward += (self.desired_speed - target_speed)

        if self._is_collision:
            reward += -100.0
        if self._is_off_road:
            reward += -100.0

        ego_loc = self.ego.get_location()
        current_dist = ego_loc.distance(self.target_destination)
        dist_change = self.prev_dist_to_goal - current_dist

        if not should_stop:
            reward += dist_change * 5.0

        self.prev_dist_to_goal = current_dist

        if self.last_closest_idx > self.last_rewarded_idx:
            passed_waypoints = self.last_closest_idx - self.last_rewarded_idx
            reward_per_point = 10.0
            current_waypoint_reward = passed_waypoints * reward_per_point
            reward += current_waypoint_reward
            # print("经过路点")
            self.last_rewarded_idx = self.last_closest_idx

        if current_dist < 2.0:
            reward += 200.0
            print("到达终点")

        return reward / 50.0

    def _get_cost(self, obs):
        cost = 0.0
        if self._is_collision:
            cost += 20.0
        if self._is_off_road:
            cost += 20.0
        speed = obs['ego_state'][3]
        if speed > self.desired_speed:
            cost += (speed - self.desired_speed) / self.desired_speed
        return cost

    def _terminal(self, obs):
        ego_transform = self.ego.get_transform()
        ego_loc = ego_transform.location

        if len(self.collision_hist) > 0:
            self._is_collision = True
            print('Collision occurred')
            return True

        if self.time_step > self.max_time_episode:
            print('Exceeded maximum timesteps')
            return True

        dist_to_goal = ego_loc.distance(self.target_destination)
        if dist_to_goal < 5.0:
            print('Goal Reached! Success!')
            return True

        waypoint = self.world.get_map().get_waypoint(
            self.ego.get_location(),
            project_to_road=True,
            lane_type=carla.LaneType.Driving
        )
        if waypoint is None:
            self._is_off_road = True
            print('Non-drivable lane detected')
            return True

        ego_yaw = self.ego.get_transform().rotation.yaw
        lane_yaw = waypoint.transform.rotation.yaw
        yaw_diff = np.deg2rad(ego_yaw - lane_yaw)
        yaw_diff = np.arctan2(np.sin(yaw_diff), np.cos(yaw_diff))
        if not waypoint.is_intersection:
            if abs(yaw_diff) > np.pi / 2:
                self._is_off_road = True
                print('Wrong-way driving detected')
                return True

        lane_width, lateral_offset, angle_diff = obs['lane_info']
        if not waypoint.is_intersection:
            if lateral_offset > lane_width / 2 + 1.0:
                self._is_off_road = True
                print('Deviated from lane')
                return True

        return False

    def _clear_all_actors(self, actor_filters):
        """使用 Batch Command 销毁指定类型的 Actor"""
        for actor_filter in actor_filters:
            actors = self.world.get_actors().filter(actor_filter)
            if not actors:
                continue
            actor_ids = [actor.id for actor in actors if actor is not None]
            self._destroy_actor_ids_batch(actor_ids, label=actor_filter)

        if self.settings.synchronous_mode:
            try:
                self.world.tick()
            except RuntimeError as e:
                print(f"Warning: world.tick() failed after clearing actors: {e}")

    def close(self):
        """环境关闭时的清理"""
        print("Closing CarlaEnv: Destroying all spawned actors...")

        # 1. 销毁传感器
        self._destroy_sensors()

        # 2. 销毁长尾场景 actor（含静态物）
        self.long_tail_event_manager.destroy(do_tick=False)

        # 3. 销毁行人
        self._destroy_walkers()

        # 4. 统一销毁全部车辆（包含 ego）
        self._clear_all_actors(['vehicle.*'])
        self.ego = None

        print("Cleanup complete.")
