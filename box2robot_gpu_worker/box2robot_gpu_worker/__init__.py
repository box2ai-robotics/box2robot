"""Box2Robot GPU Worker — LeRobot integration layer for Box2Robot platform."""
__version__ = "0.5.1"

# Servo normalization constants
STS_POS_MAX = 4095  # STS3215 encoder range
SC_POS_MAX = 1023   # SC09 encoder range
HW_POS_MAX = 4095   # Hiwonder HX (STS 兼容协议, 0-4095)
DEFAULT_FPS = 20    # Box2Robot recording sample rate
