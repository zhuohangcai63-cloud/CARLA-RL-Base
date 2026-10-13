# Bench2Drive-style parked vehicle cut-in template.

param scenario_name = "parking_cut_in"
param weather = Uniform("ClearNoon", "CloudyNoon", "WetNoon")
param traffic_light_policy = "on"

param ego_spawn_index = Range(0, 1000000)
param destination_spawn_index = Range(0, 1000000)
param min_route_distance = Range(80, 160)

param number_of_vehicles = Range(4, 12)
param number_of_walkers = Range(0, 8)
param pedestrian_cross_factor = Range(0.0, 0.2)

param long_tail_event_type = "parking_cut_in"
param direction = Uniform("auto", "left", "right")
param event_distance_min = Range(28, 34)
param event_distance_max = Range(38, 50)
param trigger_distance_min = Range(14, 18)
param trigger_distance_max = Range(22, 28)
param reaction_time = Range(3.0, 4.2)
param actor_speed_min = Range(3.0, 4.5)
param actor_speed_max = Range(6.0, 8.0)
param event_timeout_steps = Range(180, 260)
param prefer_parking_lane = True
param global_parking_fallback = True
param global_parking_fallback_count = Range(3, 5)
param global_parking_spacing = 4.0
param global_parking_min_ego_distance = 20.0
param merge_target_distance = 18.0
param allow_skip_if_incompatible = True
