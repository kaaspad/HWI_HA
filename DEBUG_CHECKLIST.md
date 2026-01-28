# Homeworks Integration Debug Checklist

## Connection Issues

### Cannot Connect to Controller

1. **Verify network connectivity**
   ```bash
   ping <controller_ip>
   telnet <controller_ip> <port>
   ```

2. **Check baud rate** (if using serial-to-IP adapter)
   - Default is 9600 baud with dip switch up
   - Custom rates require SETBAUD command and dip switch down

3. **Verify credentials**
   - Try connecting via telnet and entering `LOGIN, <username>, <password>`
   - Response should be "login successful"

4. **Check hardware handshaking**
   - Some adapters don't support hardware handshaking
   - Controller may need `SETHAND, NONE`

### Connection Drops Frequently

1. **Enable debug logging**
   ```yaml
   logger:
     default: info
     logs:
       custom_components.homeworks: debug
       custom_components.homeworks.client: debug
   ```

2. **Check health sensors**
   - `sensor.homeworks_connection_status`
   - `sensor.homeworks_reconnect_count`
   - `sensor.homeworks_poll_failures`

3. **Reduce polling frequency** if controller is overloaded

## CCO State Issues

### CCO State Not Updating

1. **Verify KLS monitoring is enabled**
   - Check logs for "Keypad led monitoring enabled"
   - Manually send `RKLS, [addr]` and verify response

2. **Check address format**
   - CCO address should be 3 parts: `[processor:link:address]`
   - Button/relay is separate (1-24)

3. **Verify button number mapping**
   - CCO relays are typically buttons 1-8
   - Some keypads use buttons 9-24 for LED feedback

4. **Test manually via service**
   ```yaml
   service: homeworks.send_command
   data:
     controller_id: lutron_homeworks
     command:
       - "RKLS, [02:06:03]"
   ```

5. **Check KLS response format**
   - Should be 24 digits
   - Each digit: 0=Off, 1=On, 2=Flash1, 3=Flash2
   - For CCOs: 1=ON (relay closed), 2=OFF (relay open)

### CCO State is Inverted

1. **Check inversion setting**
   - Edit device in options flow
   - Toggle "Invert ON/OFF"

2. **Verify wiring**
   - Some devices are wired normally-open vs normally-closed
   - Inversion compensates for wiring differences

### KLS Digit Interpretation

Example KLS response:
```
KLS, [02:06:03], 000000000222112110000000
```

Reading the state:
- Position 1-24 corresponds to LED/button 1-24
- For button 6: look at position 6 (0-indexed: 5)
- In this example: position 6 = '0' (index 5)

For CCO devices:
- Relays 1-8 typically map to positions 1-8
- Value 1 = relay closed (ON)
- Value 2 = relay open (OFF)
- Value 0 = unknown/unused

## Dimmer Issues

### Dimmer Not Responding

1. **Verify address format**
   - RPM: `[processor:link:MI:module:zone]` (5 parts)
   - D48: `[processor:link:router:bus:dimmer]` (5 parts)
   - RF: `[processor:link:1:dimmer]` (4 parts)

2. **Check DL monitoring**
   - Logs should show "Dimmer level monitoring enabled"
   - Test: `RDL, [addr]` should return `DL, [addr], <level>`

3. **Verify fade rate**
   - Very long fade times may make it seem like nothing is happening
   - Try fade rate of 0 for instant response

## Keypad Issues

### Button Presses Not Detected

1. **Verify KBMON is enabled**
   - Check logs for "Keypad button monitoring enabled"

2. **Check button number**
   - Buttons are numbered 1-24
   - See keypad button diagrams in protocol doc

### LED States Not Updating

1. **Enable LED monitoring in button config**
   - Check "Has LED Indicator" when adding button

2. **Verify KLMON is enabled**

## Diagnostic Commands

### Send via HA Service

```yaml
# Request KLS state
service: homeworks.send_command
data:
  controller_id: lutron_homeworks
  command:
    - "RKLS, [02:06:03]"

# Request dimmer level
service: homeworks.send_command
data:
  controller_id: lutron_homeworks
  command:
    - "RDL, [01:01:00:02:04]"

# Test CCO relay
service: homeworks.send_command
data:
  controller_id: lutron_homeworks
  command:
    - "CCOCLOSE, [02:06:03], 1"
    - "delay 2000"
    - "CCOOPEN, [02:06:03], 1"

# Get processor info
service: homeworks.send_command
data:
  controller_id: lutron_homeworks
  command:
    - "PROCADDR"
    - "OSREV"
```

## Log Analysis

### Key Log Messages

```
# Successful connection
Connected to Homeworks controller at 192.168.1.100:23

# KLS update received
KLS update for [02:06:03]: [0, 0, 0, 0, 0, 1, 0, ...]

# CCO state change detected
CCO 2:6:3,6 state changed: False -> True (LED=1)

# Connection lost
Connection lost
Attempting to reconnect...

# Parse error
Failed to parse message: <message> - <error>
```

### Enable Verbose Logging

```yaml
logger:
  default: info
  logs:
    custom_components.homeworks: debug
    custom_components.homeworks.client: debug
    custom_components.homeworks.coordinator: debug
```

## Common Problems and Solutions

| Problem | Likely Cause | Solution |
|---------|--------------|----------|
| All CCOs show OFF | KLS polling not working | Check KLMON enabled, verify addresses |
| CCO toggles but reverts | State derived wrong | Check button number, may need inversion |
| Dimmer works, CCO doesn't | Different address format | CCO uses 3-part address, dimmer uses 5-part |
| Entities unavailable | Connection lost | Check network, enable reconnect logging |
| Slow response | High polling load | Reduce number of polled addresses |
| Parse errors in log | Malformed responses | Check controller firmware, baud rate |

## Diagnostics Download

The integration provides a diagnostics file with:
- Controller health metrics
- Device counts
- Connection status
- Last error messages

Access via: Settings → Devices & Services → Homeworks → ... → Download Diagnostics
