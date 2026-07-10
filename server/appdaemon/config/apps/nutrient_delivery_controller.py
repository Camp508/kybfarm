import appdaemon.plugins.hass.hassapi as hass

class NutrientDeliveryController(hass.Hass):
    """
    EC-based nutrient delivery controller with mixing tank + circulation.
    
    System flow:
    - water_pump_5k runs continuously for mixing tank circulation
    - Sensors (EC, pH) are in the pipe, need pump 5K running for readings
    - When solenoids closed: water recirculates in mixing tank
    - When solenoids open: water flows to grow tanks (GT1 and GT2), overflows back to mixing tank
    """

    def initialize(self):
        self.log("[Nutrient Controller] Initializing...")
        
        # Configuration
        self.enable_entity = self.args["enable_id"]
        
        # Level sensors
        self.level_gt1_sensor = self.args["slle01_gt1_level_id"]
        self.level_gt2_sensor = self.args["slle01_gt2_level_id"]
        self.level_mx_sensor = self.args["slle01_mx_level_id"]
        
        # EC sensors
        self.ec_gt1_sensor = self.args["ec_gt1_id"]
        self.ec_gt2_sensor = self.args["ec_gt2_id"]
        self.ec_mx_sensor = self.args["ec_mx_id"]
        self.ec_target_entity = self.args["nutrient_1_ref_id"]
        
        # Peristaltic pumps (dose into mixing tank)
        self.pump1_relay = self.args["peristaltic_1_id"]  # Relay 14
        self.pump2_relay = self.args["peristaltic_2_id"]  # Relay 15
        
        # Circulation system
        self.water_pump_5k = self.args["water_pump_5k_id"]  # Relay 2 - ALWAYS ON
        self.solenoid_1 = self.args["solenoid_1_id"]  # Relay 11 - GT1
        self.solenoid_2 = self.args["solenoid_2_id"]  # Relay 12 - GT2
        
        # Flow rates (m^3/s)
        self.flow_1 = float(self.args["peristaltic_flow_1"])
        self.flow_2 = float(self.args["peristaltic_flow_2"])
        
        # SAFETY PARAMETERS
        self.level_high_limit = 30.0  # cm - close solenoid at this level
        self.level_low_limit = 28.0   # cm - reopen solenoid below this level
        
        # Control parameters
        self.ec_target = 2500
        self.ec_low_threshold = 2000
        self.ec_margin = 50
        
        # Timing - Dosing into mixing tank (aggressive mode: EC < 2000)
        self.dose_duration_pump1_aggressive = 50
        self.dose_duration_pump2_aggressive = self.dose_duration_pump1_aggressive * (self.flow_1 / self.flow_2)
        
        # Timing - Dosing into mixing tank (careful mode: EC 2000-2500)
        self.dose_duration_pump1_careful = 10
        self.dose_duration_pump2_careful = self.dose_duration_pump1_careful * (self.flow_1 / self.flow_2)
        
        # Distribution timing - how long to keep solenoids open
        self.distribution_duration = 180  # 3 minutes with solenoids open
        
        # Overall cycle time (dose + distribute + check)
        self.cycle_time_aggressive = int(max(self.dose_duration_pump1_aggressive, self.dose_duration_pump2_aggressive)) + self.distribution_duration + 30
        self.cycle_time_careful = int(max(self.dose_duration_pump1_careful, self.dose_duration_pump2_careful)) + self.distribution_duration + 30
        
        # State
        self.dosing_active = False
        self.distributing = False
        self.solenoid_1_open = False
        self.solenoid_2_open = False
        self.control_handle = None
        self.current_mode = "aggressive"
        
        # Start control loop if enabled
        if self.get_state(self.enable_entity) == "on":
            self.start_control()
        
        # Listen for enable/disable toggle
        self.listen_state(self.toggle_callback, self.enable_entity)
        
        self.log("[Nutrient Controller] Initialized")
        self.log(f"[Nutrient] Safety: High={self.level_high_limit}cm, Low={self.level_low_limit}cm")
        self.log(f"[Nutrient] Flow rates: Pump1={self.flow_1:.6f} m³/s, Pump2={self.flow_2:.6f} m³/s")
        self.log(f"[Nutrient] Distribution duration: {self.distribution_duration}s")
        self.log(f"[Nutrient] Aggressive: Dose P1={self.dose_duration_pump1_aggressive:.1f}s, P2={self.dose_duration_pump2_aggressive:.1f}s, Total cycle={self.cycle_time_aggressive}s")
        self.log(f"[Nutrient] Careful: Dose P1={self.dose_duration_pump1_careful:.1f}s, P2={self.dose_duration_pump2_careful:.1f}s, Total cycle={self.cycle_time_careful}s")

    def start_control(self):
        """Start the control loop"""
        if self.control_handle is None:
            # Turn on circulation pump - stays on continuously
            self.call_service("input_boolean/turn_on", entity_id=self.water_pump_5k)
            self.log("[Nutrient] Circulation pump (water_pump_5k) turned ON - will run continuously")
            
            self.control_handle = self.run_every(self.control_loop, "now", self.cycle_time_aggressive)
            self.log("[Nutrient Controller] Started")

    def stop_control(self):
        """Stop the control loop and turn off everything"""
        if self.control_handle is not None:
            self.cancel_timer(self.control_handle)
            self.control_handle = None
        
        self.turn_off_pumps()
        self.close_all_solenoids()
        
        # Turn off circulation pump
        self.call_service("input_boolean/turn_off", entity_id=self.water_pump_5k)
        self.log("[Nutrient] Circulation pump (water_pump_5k) turned OFF")
        
        self.log("[Nutrient Controller] Stopped")

    def toggle_callback(self, entity, attribute, old, new, kwargs):
        """Handle enable/disable toggle"""
        if new == "on":
            self.start_control()
        elif new == "off":
            self.stop_control()

    def control_loop(self, kwargs):
        """Main control loop - runs periodically"""
        if self.get_state(self.enable_entity) != "on":
            return
        
        try:
            # ========== SAFETY: Check grow tank levels ==========
            level_gt1_state = self.get_state(self.level_gt1_sensor)
            level_gt2_state = self.get_state(self.level_gt2_sensor)
            
            if level_gt1_state in [None, "unknown", "unavailable"]:
                self.log("[Nutrient SAFETY] GT1 level sensor unavailable - CLOSING SOLENOIDS")
                self.close_all_solenoids()
                return
            if level_gt2_state in [None, "unknown", "unavailable"]:
                self.log("[Nutrient SAFETY] GT2 level sensor unavailable - CLOSING SOLENOIDS")
                self.close_all_solenoids()
                return
            
            level_gt1 = float(level_gt1_state)
            level_gt2 = float(level_gt2_state)
            
            # Determine which tanks are safe for flow
            gt1_safe = level_gt1 < self.level_high_limit
            gt2_safe = level_gt2 < self.level_high_limit
            
            if not gt1_safe:
                self.log(f"[Nutrient SAFETY] GT1 HIGH: {level_gt1:.1f}cm >= {self.level_high_limit}cm")
            if not gt2_safe:
                self.log(f"[Nutrient SAFETY] GT2 HIGH: {level_gt2:.1f}cm >= {self.level_high_limit}cm")
            
            # ========== Read EC values ==========
            ec_gt1_state = self.get_state(self.ec_gt1_sensor)
            ec_gt2_state = self.get_state(self.ec_gt2_sensor)
            ec_mx_state = self.get_state(self.ec_mx_sensor)
            
            if ec_gt1_state in [None, "unknown", "unavailable"] or ec_gt2_state in [None, "unknown", "unavailable"]:
                self.log("[Nutrient] EC sensor unavailable - skipping")
                return
            
            ec_gt1 = float(ec_gt1_state)
            ec_gt2 = float(ec_gt2_state)
            ec_avg_grow = (ec_gt1 + ec_gt2) / 2.0
            
            if ec_mx_state not in [None, "unknown", "unavailable"]:
                ec_mx = float(ec_mx_state)
            else:
                ec_mx = None
            
            # Get target EC
            ec_target_state = self.get_state(self.ec_target_entity)
            if ec_target_state:
                self.ec_target = float(ec_target_state)
            
            ec_error = self.ec_target - ec_avg_grow
            
            # Log status
            mx_info = f"MX={ec_mx:.0f}" if ec_mx else "MX=N/A"
            self.log(f"[Nutrient] Levels: GT1={level_gt1:.1f}cm, GT2={level_gt2:.1f}cm | EC: GT1={ec_gt1:.0f}, GT2={ec_gt2:.0f}, {mx_info}, Avg_GT={ec_avg_grow:.0f}, Target={self.ec_target:.0f}, Error={ec_error:.0f}")
            
            # ========== Control Logic ==========
            
            if ec_error <= self.ec_margin:
                # EC at target
                self.log(f"[Nutrient] EC at target - closing solenoids, stopping dosing")
                self.turn_off_pumps()
                self.close_all_solenoids()
                return
            
            # EC below target - need to dose
            
            # Check if at least one tank can receive water
            if not (gt1_safe or gt2_safe):
                self.log("[Nutrient SAFETY] BOTH tanks at high level - cannot distribute - STOPPING")
                self.turn_off_pumps()
                self.close_all_solenoids()
                return
            
            # Determine if we need to dose into mixing tank or just distribute
            if ec_mx and ec_mx > ec_avg_grow + 100:
                # Mixing tank already has higher EC - just distribute
                self.log(f"[Nutrient] Mixing tank EC already high ({ec_mx:.0f} > {ec_avg_grow:.0f}) - DISTRIBUTING ONLY")
                self.distribute_to_tanks(gt1_safe, gt2_safe)
                
            else:
                # Need to dose nutrients into mixing tank first
                if ec_avg_grow < self.ec_low_threshold:
                    # Aggressive mode
                    self.log(f"[Nutrient] EC critically low ({ec_avg_grow:.0f} < {self.ec_low_threshold}) - AGGRESSIVE DOSE + DISTRIBUTE")
                    self.dose_and_distribute(
                        self.dose_duration_pump1_aggressive,
                        self.dose_duration_pump2_aggressive,
                        gt1_safe, gt2_safe,
                        "aggressive"
                    )
                    self.reschedule_control(self.cycle_time_aggressive, "aggressive")
                else:
                    # Careful mode
                    self.log(f"[Nutrient] EC moderate ({ec_avg_grow:.0f}) - CAREFUL DOSE + DISTRIBUTE")
                    self.dose_and_distribute(
                        self.dose_duration_pump1_careful,
                        self.dose_duration_pump2_careful,
                        gt1_safe, gt2_safe,
                        "careful"
                    )
                    self.reschedule_control(self.cycle_time_careful, "careful")
        
        except Exception as e:
            self.log(f"[Nutrient ERROR] {str(e)}")
            self.turn_off_pumps()
            self.close_all_solenoids()

    def dose_and_distribute(self, duration_pump1, duration_pump2, gt1_safe, gt2_safe, mode):
        """
        Complete dosing cycle:
        1. Close solenoids, dose nutrients into mixing tank (recirculates in MX)
        2. Wait for dosing to complete
        3. Open solenoids to distribute to grow tanks
        """
        volume = self.flow_1 * duration_pump1
        
        self.log(f"[Nutrient PHASE 1] Dosing {volume:.6f} m³ into MIXING TANK ({mode}) - Pump1: {duration_pump1:.1f}s, Pump2: {duration_pump2:.1f}s")
        
        # Ensure solenoids are closed during dosing (water recirculates in mixing tank)
        self.close_all_solenoids()
        
        # Turn both peristaltic pumps ON (dosing into mixing tank)
        self.call_service("input_boolean/turn_on", entity_id=self.pump1_relay)
        self.call_service("input_boolean/turn_on", entity_id=self.pump2_relay)
        
        # Schedule each pump to turn OFF
        self.run_in(self.turn_off_pump1, duration_pump1)
        self.run_in(self.turn_off_pump2, duration_pump2)
        
        self.dosing_active = True
        
        # After dosing completes, start distribution
        max_dose_time = max(duration_pump1, duration_pump2)
        self.run_in(lambda kwargs: self.distribute_to_tanks(gt1_safe, gt2_safe), max_dose_time + 5)

    def distribute_to_tanks(self, gt1_safe, gt2_safe):
        """
        Phase 2: Open solenoids to distribute nutrients from mixing tank to grow tanks
        water_pump_5k is already running, just need to open solenoids
        """
        self.log(f"[Nutrient PHASE 2] DISTRIBUTING for {self.distribution_duration}s (opening solenoids)")
        
        # Open solenoids only for safe tanks
        # water_pump_5k already running continuously
        
        if gt1_safe:
            self.call_service("input_boolean/turn_on", entity_id=self.solenoid_1)
            self.solenoid_1_open = True
            self.log("[Nutrient] Solenoid 1 OPEN (GT1 safe)")
        else:
            self.log("[Nutrient] Solenoid 1 CLOSED (GT1 level too high)")
        
        if gt2_safe:
            self.call_service("input_boolean/turn_on", entity_id=self.solenoid_2)
            self.solenoid_2_open = True
            self.log("[Nutrient] Solenoid 2 OPEN (GT2 safe)")
        else:
            self.log("[Nutrient] Solenoid 2 CLOSED (GT2 level too high)")
        
        self.distributing = True
        
        # Schedule solenoids to close after distribution period
        self.run_in(self.close_all_solenoids, self.distribution_duration)

    def close_all_solenoids(self, kwargs=None):
        """Close both solenoids (water recirculates in mixing tank only)"""
        self.call_service("input_boolean/turn_off", entity_id=self.solenoid_1)
        self.call_service("input_boolean/turn_off", entity_id=self.solenoid_2)
        self.solenoid_1_open = False
        self.solenoid_2_open = False
        self.distributing = False
        if kwargs is None:  # Only log if called directly, not from timer
            self.log("[Nutrient] All solenoids CLOSED (recirculating in mixing tank)")

    def turn_off_pump1(self, kwargs=None):
        """Turn pump 1 OFF"""
        self.call_service("input_boolean/turn_off", entity_id=self.pump1_relay)
        self.log("[Nutrient] Pump 1 OFF")

    def turn_off_pump2(self, kwargs=None):
        """Turn pump 2 OFF"""
        self.call_service("input_boolean/turn_off", entity_id=self.pump2_relay)
        self.log("[Nutrient] Pump 2 OFF")

    def turn_off_pumps(self, kwargs=None):
        """Turn both peristaltic pumps OFF immediately"""
        self.call_service("input_boolean/turn_off", entity_id=self.pump1_relay)
        self.call_service("input_boolean/turn_off", entity_id=self.pump2_relay)
        self.dosing_active = False

    def reschedule_control(self, new_cycle_time, mode):
        """Reschedule the control loop with a new cycle time"""
        if self.current_mode == mode:
            return
        
        self.current_mode = mode
        
        if self.control_handle is not None:
            try:
                self.cancel_timer(self.control_handle)
            except:
                pass
        
        self.control_handle = self.run_every(self.control_loop, f"now+{new_cycle_time}", new_cycle_time)
        self.log(f"[Nutrient] Switched to {mode} mode: {new_cycle_time}s cycle")

    def terminate(self):
        """Clean up when app terminates"""
        self.stop_control()