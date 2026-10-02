import I2C_LCD_driver, obd, time, math, threading, os
from obd import OBDStatus

"""
She's got some plans.
 - Ideally the Pi would be rewired to handle shutdowns more safely. Right now, we expect power to be cut out at any 
moment, and so we keep IO operations to a minimum. In the future, we should use a buck converter and an add-a-circuit fuse to
hook into the car's fusebox, and let us detect when the car is turned off. 
 - We might want some threading. LCD displays are currently blocking, this sucks. It might be best to have some file 
 operations put in a thread, or background some of the queries, such as iMPG, so we can have a more accurate aMPG reading.
 This might need a rewrite of most of this code. One single thread to handle OBD queries is the way to go.
 - Serve some data over a simple http server. Having web interface to show data on a phone connected to the Pi's hotspot could be useful for adjusting settings for 
 what to display on the LCD, or to show graphs of data collected over time.
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
    Next, it'll read and dump every code the car says it supports. Last, it'll get a sample pool of data.
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


def get_speed():
    try:
        return adapter.query(obd.commands.SPEED, force=True)
    except Exception as e:
        print(f"Error occurred while fetching speed: {e}")
        return None


def get_rpm():
    try:
        return adapter.query(obd.commands.RPM, force=True)
    except Exception as e:
        print(f"Error occurred while fetching RPM: {e}")
        return None


def get_maf():
    try:
        return adapter.query(obd.commands.MAF, force=True)
    except Exception as e:
        print(f"Error occurred while fetching MAF: {e}")
        return None


def get_equiv_ratio():
    try:
        return adapter.query(obd.commands.COMMANDED_EQUIV_RATIO, force=True)
    except Exception as e:
        print(f"Error occurred while fetching equiv ratio: {e}")
        return None


def get_fuel_level():
    try:
        return adapter.query(obd.commands.FUEL_LEVEL, force=True)
    except Exception as e:
        print(f"Error occurred while fetching fuel level: {e}")
        return None


def get_coolant_temp():
    try:
        return adapter.query(obd.commands.COOLANT_TEMP, force=True)
    except Exception as e:
        print(f"Error occurred while fetching coolant temp: {e}")
        return None


def get_runtime():
    try:
        return adapter.query(obd.commands.RUN_TIME, force=True)
    except Exception as e:
        print(f"Error occurred while fetching runtime: {e}")
        return None


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
    time.sleep(3)
    adapter = obd.OBD()

# If initial loop is exited we must be good to go, open new file for writing
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
impg_arr = []
ampg = None
loop_count = 0
while True:
    loop_count += 1
    # Calculate gear
    lcd_msg("Predicted gear:")
    for _ in range(10):
        gear = calculate_gear(get_speed().value.magnitude, get_rpm().value.magnitude)
        lcd.lcd_display_string(gear, 2)
        time.sleep(0.5)

    # Instant MPG
    impg = None
    lcd_msg("Instant MPG:")
    # formula from https://manuals.plus/m/8f08573961e7c5e83133532cdd853b80026fa4487393a7c52304287d758e9f39
    for _ in range(5):
        speed_mph = get_speed().value.magnitude
        if speed_mph > 0 and get_rpm().value.magnitude > 0:
            impg = (
                (14.7 / get_equiv_ratio().value.magnitude) * 6.1738 * 454 * speed_mph
            ) / (3600 * get_maf().value.magnitude)
            impg = min(impg, 99.9)
            lcd.lcd_display_string(str(round(impg, 2)), 2)
            impg_arr.append(impg)
        else:
            lcd.lcd_display_string("----", 2)
        time.sleep(1)

    # Average MPG (using currently accumulated iMPG values)
    # TODO: This is bad. The impg_arr value can theoretically grow indefinitely, and this will become more costly to calculate over time.
    # We should probably using a rolling average instead.
    if len(impg_arr) > 0:
        ampg = min(sum(impg_arr) / len(impg_arr), 99.9)
        lcd_msg("Average MPG:", (round(ampg, 2)))
    else:
        lcd_msg("Average MPG:", "----")
    time.sleep(5)

    # Coolant temp
    lcd_msg("Coolant temp:")
    for _ in range(5):
        lcd.lcd_display_string(str(get_coolant_temp().value.magnitude) + "C", 2)
        time.sleep(1)

    # Fuel level
    # TODO: In Oakley's car, this value was jumping around like crazy. Cluster showed around 45%, while the display read anywhere from 60% - 45%.
    # Is this reading accurate while in motion? Probably doesn't account for slosh. An average of the last few readings would likely be
    # better, or perhaps we only read the fuel level when the car is travelling slow enough.
    if get_speed().value.magnitude < 3:
        lcd_msg("Fuel level:", str(round(get_fuel_level().value.magnitude, 1)) + "%")
        time.sleep(5)

    # Car trip stats, write aMPG and fuel levels to file.
    # Since we can't safely handle shutdowns, we just write to the file every fifth loop and hope we don't lose power mid-write.
    # This suuuuucks.
    """
    if loop_count % 5 == 0:
        fh.write(
            str(round(ampg, 2))
            + ","
            + str(round(get_fuel_level().value.magnitude, 1))
            + "\n"
        )
        fh.flush()
        """
