# Bench2Drive-style construction obstacle template.

param scenario_name = "construction_obstacle"
param weather = Uniform("CloudyNoon", "WetNoon", "MidRainyNoon")
param traffic_light_policy = "on"

param ego_spawn_index = Range(0, 1000000)
param destination_spawn_index = Range(0, 1000000)
param min_route_distance = Range(70, 150)

param number_of_vehicles = Range(2, 8)
param number_of_walkers = Range(0, 8)
param pedestrian_cross_factor = Range(0.0, 0.2)

param long_tail_event_type = "construction_obstacle"
param event_distance_min = Range(35, 45)
param event_distance_max = Range(60, 85)
param event_timeout_steps = Range(500, 900)
param global_construction_fallback = True
param global_construction_fallback_count = Range(2, 4)
param global_construction_spacing = 8.0
param global_construction_min_ego_distance = 25.0
param global_construction_straight_yaw_tolerance = 8.0
param global_construction_straight_check_distance = 12.0
param allow_skip_if_incompatible = True
