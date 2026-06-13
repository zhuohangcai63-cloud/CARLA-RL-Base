# Bench2Drive-style side cut-in template.

param scenario_name = "side_cut_in"
param weather = Uniform("ClearNoon", "CloudyNoon", "WetNoon")
param traffic_light_policy = "on"

param ego_spawn_index = Range(0, 1000000)
param destination_spawn_index = Range(0, 1000000)
param min_route_distance = Range(80, 160)

param number_of_vehicles = Range(18, 30)
param number_of_walkers = Range(0, 8)
param pedestrian_cross_factor = Range(0.0, 0.2)

param long_tail_event_type = "side_cut_in"
param direction = Uniform("auto", "left", "right")
param event_distance_min = Range(25, 35)
param event_distance_max = Range(45, 65)
param trigger_distance_min = Range(10, 14)
param trigger_distance_max = Range(18, 26)
param reaction_time = Range(2.0, 3.0)
param actor_speed_min = Range(5.0, 7.0)
param actor_speed_max = Range(9.0, 13.0)
param event_timeout_steps = Range(180, 260)
param allow_skip_if_incompatible = True
