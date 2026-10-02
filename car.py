import I2C_LCD_driver, obd, time, math, threading, os
from obd import OBDStatus

"""
She's got some plans.
 - Ideally the Pi would be rewired to handle shutdowns more safely. Right now, we expect power to be cut out at any 
moment, and so we keep IO operations to a minimum. In the future, we should use a buck converter and an add-a-circuit fuse to
hook into the car's fusebox, and let us detect when the car is turned off. 
 - Serve some data over a simple http server. Having a web interface to show data on a phone connected to the Pi's hotspot 
 could be useful for adjusting settings for what to display on the LCD, or to show graphs of data collected over time.
 """

###
# Values specific for 2009 Honda Accord EX-L 2.4L 4cyl
GEAR_RATIOS = {"1": 2.652, "2": 1.614, "3": 1.082, "4": 0.773, "5": 0.566}
FINAL_DRIVE = 4.44
TIRE_DIAMETER = 25.8583  # P225/50 R17
###

TIRE_CIRCUMFERENCE = TIRE_DIAMETER * math.pi

lcd = I2C_LCD_driver.lcd()


def lcd_msg(l1="", l2=""):
    """Clear the LCD and display two lines of text."""
    lcd.lcd_clear()
    lcd.lcd_display_string(str(l1)[:16], 1)
    lcd.lcd_display_string(str(l2)[:16], 2)


def calculate_gear(speed_mph, rpm):
    """This isn't very accurate. Just for fun.
    TODO: Can we make this more accurate?
    """
    if speed_mph <= 5:
        return "?"

    wheel_rpm = speed_mph * 63360 / (TIRE_CIRCUMFERENCE * 60)

    theGear = None
    prediction = float("inf")
    for gear, ratio in GEAR_RATIOS.items():
        expected_rpm = wheel_rpm * ratio * FINAL_DRIVE
        error = abs(rpm - expected_rpm)

        if error < prediction:
            prediction = error
            theGear = gear
    return theGear


def dump(adapter):
    """
    This does two things. First, we read the VIN to create a folder to hold the corresponding dump. This is an attempt to make this cross-carpatible.
    Next, it'll read and dump every code the car says it supports. Last, it'll get a sample pool of data. This should be run before doing ANY work, as
    it'll conflict with the worker threads otherwise.
    """
    vin = adapter.query(obd.commands.VIN)

    # Create a folder for the car's data
    if not os.path.exists(f"{vin}"):
        lcd_msg("New VIN", "Dumping...")
        os.mkdir(f"{vin}")
        # Dump the car's supported commands to an external file
        commands = sorted(adapter.supported_commands, key=str)
        with open(f"{vin}/supported_commands.txt", "w") as f:
            for command in commands:
                f.write(f"{command}\n")

        # Dump sample data of data for every PID the car says it supports
        with open(f"{vin}/sample_dump.txt", "w") as f:
            for command in commands:
                try:
                    response = adapter.query(command)

                    f.write("===========" + "\n")
                    f.write(f"Command : {command.name}\n")
                    f.write(f"Value   : {response.value}\n")
                    f.write(f"Units   : {response.unit}\n")
                    f.write(f"Raw     : {response}\n")

                except Exception as e:
                    f.write("===========" + "\n")
                    f.write(f"Command : {command.name}\n")
                    f.write(f"ERROR   : {e}\n")

    else:
        print(f"Data for VIN {vin} already exists. Skipping dump.")


lock = threading.Lock()
stop_event = threading.Event()

state = {
    "speed": None,
    "rpm": None,
    "maf": None,
    "equiv_ratio": None,
    "fuel_level": None,
    "coolant_temp": None,
    "runtime": None,
    "sample_id": 0,  # Used to determine sample freshness
    # These values should never exceed 99.99 due to min() in mpg_worker().
    "impg": None,
    "ampg": None,
}


def obd_worker():
    """THREAD: Query the OBD adapter for all data we want to read (speed, rpm, maf, equiv, fuel level, runtime), and
    update the global state dict with raw values. The adapter can only receive one query at a time, so we need
    to lock this, as well as ensure we're not querying the adapter elsewhere. Increment sample_id.
    """
    while not stop_event.is_set():
        with lock:
            mpg_inputs_succeeded = True
            for key, getter in (
                ("speed", get_speed),
                ("rpm", get_rpm),
                ("maf", get_maf),
                ("equiv_ratio", get_equiv_ratio),
                ("fuel_level", get_fuel_level),
                ("coolant_temp", get_coolant_temp),
                ("runtime", get_runtime),
            ):
                value = getter()
                if value is not None:
                    state[key] = value
                elif key in ("speed", "maf", "equiv_ratio"):
                    mpg_inputs_succeeded = False
            if mpg_inputs_succeeded:
                state["sample_id"] += 1
        time.sleep(0.5)  # Adjust sleep as needed


def mpg_worker():
    """THREAD: Calculate instant MPG based on speed, maf, and equiv ratio values. The function also compares sample IDs
    to ensure that data is only calculated when samples are guaranteed fresh."""
    # formula from https://manuals.plus/m/8f08573961e7c5e83133532cdd853b80026fa4487393a7c52304287d758e9f39
    impg_sample_count = 0
    last_sample_id = 0
    while not stop_event.is_set():
        with lock:
            if state["sample_id"] != last_sample_id:
                last_sample_id = state["sample_id"]
                if (
                    state["speed"] is not None
                    and state["speed"] > 0
                    and state["maf"] is not None
                    and state["maf"] > 0
                    and state["equiv_ratio"] is not None
                    and state["equiv_ratio"] > 0
                ):
                    impg = (
                        (14.7 / state["equiv_ratio"]) * 6.1738 * 454 * state["speed"]
                    ) / (3600 * state["maf"])
                    impg = min(impg, 99.99)
                    state["impg"] = impg
                    impg_sample_count += 1
                    if state["ampg"] is None:
                        state["ampg"] = impg
                    else:
                        state["ampg"] += (impg - state["ampg"]) / impg_sample_count
                    state["ampg"] = min(state["ampg"], 99.99)
                else:
                    state["impg"] = None
        time.sleep(0.5)


# Getters
# These all return the raw values of each query, no units included. If a query fails, None is returned.
def get_speed():
    try:
        return adapter.query(obd.commands.SPEED, force=True).value.magnitude
    except Exception as e:
        print(f"Error occurred while fetching speed: {e}")
        return None


def get_rpm():
    try:
        return adapter.query(obd.commands.RPM, force=True).value.magnitude
    except Exception as e:
        print(f"Error occurred while fetching RPM: {e}")
        return None


def get_maf():
    try:
        return adapter.query(obd.commands.MAF, force=True).value.magnitude
    except Exception as e:
        print(f"Error occurred while fetching MAF: {e}")
        return None


def get_equiv_ratio():
    try:
        return adapter.query(
            obd.commands.COMMANDED_EQUIV_RATIO, force=True
        ).value.magnitude
    except Exception as e:
        print(f"Error occurred while fetching equiv ratio: {e}")
        return None


def get_fuel_level():
    try:
        return adapter.query(obd.commands.FUEL_LEVEL, force=True).value.magnitude
    except Exception as e:
        print(f"Error occurred while fetching fuel level: {e}")
        return None


def get_coolant_temp():
    try:
        return adapter.query(obd.commands.COOLANT_TEMP, force=True).value.magnitude
    except Exception as e:
        print(f"Error occurred while fetching coolant temp: {e}")
        return None


def get_runtime():
    try:
        return adapter.query(obd.commands.RUN_TIME, force=True).value.magnitude
    except Exception as e:
        print(f"Error occurred while fetching runtime: {e}")
        return None


### MAIN
# Initialize LCD and attempt to connect to OBD adapter, if not detected, keep trying
lcd_msg("Initializing...")
adapter = obd.OBD()
while adapter.status() is not OBDStatus.CAR_CONNECTED:
    match adapter.status():
        case OBDStatus.NOT_CONNECTED:
            lcd_msg("Adapter not", "detected...")
        case OBDStatus.ELM_CONNECTED:
            lcd_msg("Adapter detected", "No car connected")
        case OBDStatus.OBD_CONNECTED:
            lcd_msg("Adapter detected", "No ECU response")
    time.sleep(2)
    adapter = obd.OBD()

# If initial loop is exited we must be good to go, dump info for VIN, and open new file for writing
print("We're ready, go go go...")

dump(adapter)

if not os.path.exists("trips"):
    os.mkdir("trips")
file_increment = 1
while os.path.exists("trips/trip-%s.txt" % file_increment):
    file_increment += 1
fh = open("trips/trip-%s.txt" % file_increment, "w")
print("Writing to ", os.getcwd(), "trips/trip-%s.txt" % file_increment)
lcd_msg("Connected!", "Reading...")
lcd.lcd_clear()

# TODO: Change this to handle adapter detachments after the first loop?
# If the adapter is unplugged mid-loop the script crashes and systemd handles a restart...
# like, this works?? but definitely not the best way to do this.

obd_thread = threading.Thread(target=obd_worker, daemon=True)
obd_thread.start()
mpg_thread = threading.Thread(target=mpg_worker, daemon=True)
mpg_thread.start()

loop_count = 0
while True:
    loop_count += 1
    # Calculate gear
    # We're going to comment this out for now because it conflicts with the obd_thread
    # lcd_msg("Predicted gear:")
    # for _ in range(10):
    #     gear = calculate_gear(state["speed"], state["rpm"])
    #     lcd.lcd_display_string(gear, 2)
    #     time.sleep(0.5)

    # Instant MPG
    lcd_msg("Instant MPG:")
    # formula from https://manuals.plus/m/8f08573961e7c5e83133532cdd853b80026fa4487393a7c52304287d758e9f39
    for _ in range(5):
        if state["impg"] is not None:
            lcd.lcd_display_string(str(state["impg"]), 2)
        else:
            lcd.lcd_display_string("----", 2)
        time.sleep(1)

    # Average MPG (calculated through mpg_worker())
    lcd_msg("Average MPG:")
    for _ in range(5):
        if state["ampg"] is not None:
            lcd.lcd_display_string(str(round(state["ampg"], 2)), 2)
        else:
            lcd.lcd_display_string("----", 2)
        time.sleep(1)

    # Coolant temp
    lcd_msg("Coolant temp:")
    for _ in range(5):
        if state["coolant_temp"] is not None:
            lcd.lcd_display_string(str(state["coolant_temp"]) + "C", 2)
        else:
            lcd.lcd_display_string("----", 2)
        time.sleep(1)

    # Fuel level
    # TODO: In Oakley's car, this value was jumping around like crazy. Cluster showed around 45%, while the display read anywhere from 60% - 45%.
    # Is this reading accurate while in motion? Probably doesn't account for slosh. An average of the last few readings would likely be
    # better, or perhaps we only read the fuel level when the car is travelling slow enough.
    # Right now, it'll only display the fuel level when we're going slow enough, since any other time, it's probably unreliable.
    if (
        state["speed"] is not None
        and state["speed"] < 3
        and state["fuel_level"] is not None
    ):
        lcd_msg("Fuel level:", str(round(state["fuel_level"], 1)) + "%")
        time.sleep(5)

    # Car trip stats, write aMPG and fuel levels to file.
    # Since we can't safely handle shutdowns, we just write to the file every fifth loop and hope we don't lose power mid-write.
    # This suuuuucks.
    if loop_count % 5 == 0:
        fh.write(
            str(round(state['runtime'], 2))
            + ","
            str(round(state['ampg'], 2))
            + ","
            + str(round(state['fuel_level'], 1))
            + "\n"
        )
        fh.flush()
