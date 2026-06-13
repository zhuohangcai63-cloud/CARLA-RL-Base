# Scenic template for the Bench2Drive-style HardBreakRoute scenario.

param scenario_name = "hard_break_route"
param weather = Uniform("ClearNoon", "CloudyNoon", "WetNoon")
param traffic_light_policy = "off"

param ego_spawn_index = Range(0, 1000000)
param destination_spawn_index = Range(0, 1000000)
param min_route_distance = Range(70, 130)

# Keep the scene focused on the scripted lead vehicle.
param number_of_vehicles = 0
param number_of_walkers = 0
param pedestrian_cross_factor = 0.0
param forced_crossing_walkers = 0
param forced_crossing_max_total = 0
param forced_crossing_trigger_prob = 0.0

# HardBreakRoute uses a dedicated lead vehicle spawned on ego's lane.
param hard_break_route_enabled = True
param hard_break_lead_spawn_distance_min = Range(38, 42)
param hard_break_lead_spawn_distance_max = Range(46, 50)

# The lead vehicle is tracked, then braked hard, held, and resumed.
param lead_brake_enable_prob = 1.0
param lead_brake_warmup_steps = Range(30, 50)
param lead_candidate_distance_min = 1.0
param lead_candidate_distance_max = 100.0
param lead_candidate_speed_min = 0.0
param lead_candidate_speed_max = 30.0
param lead_track_min_steps = Range(3, 6)
param lead_trigger_distance_min = 8.0
param lead_trigger_distance_max = 70.0
param lead_brake_strength = 1.0
param lead_brake_hold_steps = Range(180, 240)
param lead_brake_min_ego_speed = Range(3.5, 4.5)
param lead_event_max_per_episode = 1
