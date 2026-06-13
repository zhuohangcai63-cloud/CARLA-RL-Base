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
param trigger_distance_min = Range(8, 10)
param trigger_distance_max = Range(12, 16)
param reaction_time = Range(2.0, 2.8)
param actor_speed_min = Range(3.0, 4.5)
param actor_speed_max = Range(6.0, 8.0)
param event_timeout_steps = Range(180, 260)
param allow_skip_if_incompatible = True
