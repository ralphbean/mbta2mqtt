#!/usr/bin/python3 -u
import logging
import os,sys
import re
import logging, logging.config
import json
import requests
import queue
import yaml
from yaml_env_tag import construct_env_tag
import paho.mqtt.client as mqtt
import threading
from mergedeep import merge,Strategy

VERSION='0.1.0'

# Keep track of messages for creating Home Assistant entities via MQTT discovery.
# Since these are retained by the broker, we should get the list of any already
# existing ones when start... and we can clean those up when we get a 'reset' from
# the MBTA streaming API (which happens on initial connection). This should keep us
# from having lingering zombie entities in Home Asisstant.
entities = queue.SimpleQueue()


def main():

    # Load YAML config files
    # Log config chicken/egg problem resolved by
    # storing info to log later!
    (config,config_log)=load_config()

    # Set up logging as per config file.
    # These extra level names match
    # https://verboselogs.readthedocs.io/en/latest/readme.html#overview-of-logging-levels
    # except "TRACE" instead of "SPAM"
    # https://github.com/xolox/python-verboselogs/issues/11
    logging.addLevelName( 5, 'TRACE')
    logging.addLevelName(15, 'VERBOSE')
    logging.addLevelName(25, 'NOTICE')
    try:
        logging.config.dictConfig(config['logger'])
    except ValueError as ex:
        logging.critical(f"Config: error configuring logging. ({ex.__cause__})")
        exit(1)
    logging.log(25,f"::::: Starting mbta2mqtt v{VERSION}'")

    # spit out any messages saved from loading the config.
    for (loglevel,logmessage,rc) in config_log:
        logging.log(loglevel,logmessage)
        if rc > 0:
            exit(rc)


    # Validate and log some key things.
    rc=check_config(config)
    if rc > 0:
        exit(rc)

    # Any stops on the command line _override_ the
    # config file!
    if sys.argv[1:]:
        config['mbta']['stops'] = sys.argv[1:]
        logging.log(25,f"Arg!: Got stop configuration from command line.")

    if 'stops' not in config['mbta'] or type(config['mbta']['stops']) != list:
       logging.critical(f"Config: Need a list of MBTA stops, either in the config or on the command line.")
       exit(1)

    # TODO: check if the stops are valid before continuing.
    # As currently written, the stops are given as a filter,
    # and if nothing ever matches because your stop id is
    # wrong, you'll just get _nothing_.
    logging.debug(f"Config: Note that the stop list is not (currently) validated.")

    # Construct the MBTA API request URL based on the config
    try:
        url = ( f"{config['mbta']['server']}"
                f"{config['mbta']['endpoint']}"
                f"?filter[stop]={','.join(config['mbta']['stops'])}"
                f"&include={','.join(config['mbta']['include'])}"
              )
    except KeyError as ex:
        logging.critical(f"Config: Could not construct MBTA API header URL. (Is {ex} defined in the mbta: section?)")
        exit(1)
    except TypeError as ex:
        logging.critical(f"Config: Could not construct MBTA API request URL. (Are numbers in quotes in the config file?): {ex}")
        exit(1)

    # Construct the MBTA API request headers based on the config
    try:
        if not re.match('^[0-9a-f]{32}$',config['mbta']['api_key']):
            raise ValueError(config['mbta']['api_key'])
        headers = {"X-API-Key": config['mbta']['api_key'], "Accept": "text/event-stream"}
    except KeyError as ex:
        logging.critical(f"Config: Could not construct MBTA API header. (Is {ex} defined in the mbta: section?)")
        exit(1)
    except TypeError as ex:
        if not config['mbta']['api_key']:
            logging.critical(f"Config: Could not construct MBTA API request header. (Check the 'api-key: !ENV ...' environment variable in your config file.)")
        else:
            logging.critical(f"Config: Could not construct MBTA API request header. (Get an API key from Get from: https://api-v3.mbta.com/register): {ex}")
        exit(1)
    except ValueError as ex:
        logging.critical(f"Config: MBTA v3 API key doesn't look right. Get from: https://api-v3.mbta.com/register ('{ex}' should be a 32-byte hex value.)")
        exit(1)

    logging.debug(f"MBTA: API request URL: '{url}'")
    logging.log(5,f"MBTA: API request headers: '{headers}'")


    # Start the MQTT client. `loop_start()` runs a thread
    # in the background handling this, so we can keep our
    # main _recieve_ loop... looping.
    mqttc = mqtt.Client(userdata=config)
    mqttc.on_connect = mqtt_connect
    mqttc.on_disconnect = mqtt_disconnect
    mqttc.on_publish = mqtt_publish
    try:
        mqttc.connect(config['mqtt']['host'],
                  port=config['mqtt']['port'],
                  keepalive=config['mqtt']['keepalive'])
    except OSError as ex:
        logging.critical(f"Could not connect to MQTT Broker '{config['mqtt']['host']}:{config['mqtt']['port']}': {ex}")
        exit(1)
    mqttc.loop_start()
    logging.log(15,f"MQTT client connected to '{config['mqtt']['host']}:{config['mqtt']['port']}'")


    # set ourselves as online
    mqttc.publish(topic=f"{config['mqtt']['prefix']}/status",payload="online",qos=1,retain=True).wait_for_publish()

    # "last will" message — if we're disconnected, this should
    # be sent automatically
    mqttc.will_set(topic=f"{config['mqtt']['prefix']}/status",payload="offline",qos=1,retain=True)

    # Subscribe to our own Home Assistant discovery topics. We need this so
    # we can clean them up when they're no longer valid. (Like, when we get a 
    # "reset" event.) The lock is how we wait for the broker to acknowledge
    # our subscription.
    discovery_wildcard = f"{config['homeassistant']['discovery_prefix']}/+/{config['homeassistant']['node_id']}/+/config"
    mqttc.message_callback_add(discovery_wildcard, mqtt_discovery_message)
    mqtt_subscribe_wait(mqttc, discovery_wildcard)

    # Pre-compiled regex for splitting individual events from the streaming api.
    # See:
    # *  https://www.mbta.com/developers/v3-api/streaming
    # * https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events
    eventsplit = re.compile('^(?:: keep-alive\n)*event: (.*)\ndata: (.*)')

    # And here's the main loop — connect, process events, publish!
    requests_session = requests.Session()
    rc=0
    try:
        with requests_session.get(url, headers=headers, stream=True, timeout=(30.05,60)) as result:

            result.raise_for_status()

            logging.log(25,f"MBTA: Connected to API stream at '{config['mbta']['server']}{config['mbta']['endpoint']}'")

            # the server-sent events are separated by blank lines.
            for chunk in result.iter_lines(delimiter=b'\n\n'):

                # There seem to be "blank" messages at the end of every batch.
                # We're just skipping those.
                if chunk == b'':
                    logging.log(5,"MBTA --------------------------------------------------------------------------------")
                    continue

                # Split chunks from the stream into individual events 
                try:
                    event, data = eventsplit.match(chunk.decode('UTF-8')).groups()
                except:
                    logging.warning("Skipping an entirely unexpected response from the MBTA streaming API.")
                    logging.debug(f"'{chunk.decode('UTF-8')}'")
                    continue


                logging.log(15,f"MBTA {event} event")
                logging.log(5,f"MBTA {event} json: \"{data}\"")

                try:
                    resource = json.loads(data)
                except json.decoder.JSONDecodeError as ex:
                    logging.warning(f"MBTA JSON response not decoded. {ex}")
                    continue

                match event:
                    case "reset":
                        # Need to:
                        #   1. Clear all existing mqtt entries
                        #   2. Loop through and add the individual resources
                        reset_entities(mqttc)
                        # "resource" is actually plural in this case
                        for r in resource:
                            add_entity(config,mqttc,r)
                    case "add":
                        # Add a single entity
                        add_entity(config,mqttc,resource)
                    case "update":
                        # just update existing entity
                        update_entity(config,mqttc,resource)
                    case "remove":
                        # Clear a single entity
                        remove_entity(config,mqttc,resource)
                    case "error":
                        # Something's wrong!
                        logging.critical(f"MBTA: Responded with error: {resource['errors'][0]['code']} ({resource['errors'][0]['status']})")
                    case _:
                        logging.warning(f"MBTA event {event} not recognized!")
                        logging.debug(f"MBTA unknown event: {chunk.decode('UTF-8')}")
    except requests.RequestException as ex:
        # lumping these all together because we want to do the same
        # thing in any case: exit.
        logging.critical(f"MBTA: Error accessing the MBTA API: {ex}")
        rc=2
    except KeyboardInterrupt:
        logging.info(f"::::: Keyboard interrupt. Shutting down.")

    logging.debug(f"::::: Cleanup initiated.")
    reset_entities(mqttc)
    mqttc.publish(topic=f"{config['mqtt']['prefix']}/status",payload="offline",qos=1,retain=True).wait_for_publish()
    mqttc.disconnect()
    logging.log(25,f"::::: Exited cleanly.")


def load_config():
    """Loads `defaults.conf` and other files defined there,
       if any and if found.
    """


    # First defaults, and then merge any others.
    # Since logging can be configured, there
    # is a chicken-and-egg thing so... 
    # config file problems will be logged later!
    config_log = []
    # the bare minimum the logger needs to even
    # log failure!
    config = {}
    config['logger'] = {}
    config['logger']['version'] = 1

    os.chdir(os.path.split(sys.argv[0])[0])
    defaultfile="defaults.conf"

    # This lets us indicate environment variables in
    # the config file. Handy!
    yaml.Loader.add_constructor('!ENV', construct_env_tag)

    try:
        with open(defaultfile) as cf:
           config = yaml.load(cf, Loader=yaml.Loader)
    except FileNotFoundError as ex:
        config_log.append((50,f"Config: Could not load 'defaults.conf'. We need that! It should be at '{defaultfile}' ({ex})",1))
        return (config,config_log)
    except yaml.scanner.ScannerError as ex:
        config_log.append((50,f"Config: Error parsing defaults. Since that shouldn't happen, bailing out now. ({ex})",1))
        return (config,config_log)
    else:
        config_log.append((20,f"Config: Loaded defaults from {os.path.split(sys.argv[0])[0]}/{defaultfile}",0))
    
    if "configpath" in config:
        config_log.append((5,f"Config: Looking for these files: {config['configpath']}",0))
        for configfile in config['configpath']:
            try:
                with open(configfile) as cf:
                    additional_config = yaml.load(cf, Loader=yaml.Loader)
            except FileNotFoundError:
                config_log.append((10,f"Config: {configfile} not found. Skipping.",0))
            except IsADirectoryError:
                config_log.append((40,f"Config: Error: config file is a directory rather than a file! ('{configfile}')" ,0))
            except yaml.scanner.ScannerError as ex:
                config_log.append((40,f"Config: Error parsing config file! Skipping. ({ex})",0))
            else:
                if additional_config:
                    merge(config,additional_config, strategy=Strategy.ADDITIVE)
                    config_log.append((20,f"Config: Loaded configuration from {configfile}",0))
                else:
                    config_log.append((30,f"Config: {configfile} found, but it seems empty. Hope that's okay.",0))
            if "endconfig" in config and config["endconfig"]:
                config_log.append((10,f"Config: Found 'endconfig' key in {configfile}. Will not look at later configuration files.",0))
                break


    return (config,config_log)

def check_config(config):
    rc=0

    vitals = {
        "mbta": ( "api_key", "server", "endpoint","include"),
        "mqtt": ("host", "port", "prefix", "keepalive" ),
        "homeassistant": ("discovery_prefix","node_id","entity")

    }

    for section in vitals.keys():
        if section not in config:
            rc=1
            logging.critical(f"Config: missing '{section}' section!")
        elif type(config[section]) != dict:
            rc=1
            logging.critical(f"Config: section '{section}' is not a dictionary. (Do you have a config file with everything but the top level commented out?)")
        else:
            for vital in vitals[section]:
                if vital not in config[section]:    
                    rc=1
                    logging.critical(f"Config: '{section}' section missing required key '{vital}'")

    return(rc)


def mqtt_connect(client, userdata, flags, rc):
    """Called when the mqtt client connects."""
    if rc != 0:
        logging.critical(f"MQTT: Could not connect to Broker '{userdata['mqtt']['host']}:{userdata['mqtt']['port']}' — return code {rc}")
        exit(1)
    logging.log(25,f"MQTT: Connected to Broker '{userdata['mqtt']['host']}:{userdata['mqtt']['port']}'")


def mqtt_disconnect(client, userdata, rc):
    """Called when the mqtt client disconnects, either intentionally or not."""
    if rc != 0:
        logging.critical(f"MQTT: Lost connection to Broker '{userdata['mqtt']['host']}:{userdata['mqtt']['port']}' ({mqtt.error_string(rc)})")
        os._exit(3)
    logging.info(f"MQTT: Disconnected from Broker '{userdata['mqtt']['host']}:{userdata['mqtt']['port']}'")    

def mqtt_publish(client, userdata, mid):
    logging.log(5,f"MQTT: message sent for publication ({mid})")  

def mqtt_discovery_message(client, userdata, message):
    """Handles Home Assistant discovery messages.
       Specifically: stash them in the `entities` queue
       so we can clean them up later if asked.
    """

    try:
        payload = str(message.payload.decode('utf-8'))
    except UnicodeDecodeError as ex:
        logging.warning(f"MQTT: Received message with topic '{message.topic}' and non-unicode payload! ({ex})")
        logging.log(5,f"MQTT: Weird payload for '{message.topic}' is payload: '{message.payload}')")
        return

    logging.log(5,f"MQTT: Received message (topic: '{message.topic}', payload: '{payload}')")


    if not re.match(f"^{userdata['homeassistant']['discovery_prefix']}/[a-z0-9_-]+/[A-Za-z0-9_/-]+/config$",message.topic):
        logging.warning(f"MQTT: Got a message that doesn't look like a Home Assistant discovery topic ('{message.topic}')!")
        return
    
    if payload == '':
        logging.log(5,f"MQTT: Skipping Home Assistant Discovery empty message ('{message.topic}')")
        return

    logging.debug(f"MQTT: Found Home Assistant Discovery Topic '{message.topic}'")
    entities.put(message.topic)


def mqtt_subscribe_wait(client, topic):
    """Subcribe to a topic and wait for acknowledgement."""

    subscribe_lock = threading.Lock()
    subscribe_lock.acquire(blocking=False)
    
    def on_subscribe(client, userdata, mid, granted_qos):
        """Called when the subscription succeeds."""
        logging.log(5,f"MQTT: Subscription succeeded with message id {mid} and qos {granted_qos}")
        subscribe_lock.release()
    
    client.on_subscribe = on_subscribe
    (result, mid) = client.subscribe(topic)
    if result == mqtt.MQTT_ERR_SUCCESS:
        logging.log(5,f"MQTT: Requested subscription to '{topic}' with message id {mid}")
    else:
        logging.critical(f"MQTT: Subscription request failed: '{mqtt.error_string(result)}'")

    # now wait for the lock to be cleared
    if subscribe_lock.acquire(blocking=True,timeout=30):
        logging.debug(f"MQTT: Subscribed to '{topic}' with message id {mid}")
    else:
        logging.critical(f"MQTT: Failed to subscribe to '{topic}'")



def reset_entities(client):

    logging.debug(f"MBTA: reset all resources")

    try:
        while True:
            entity = entities.get(block=False)
            logging.debug(f"MQTT: Clearing {entity}")
            # MQTT convention: we send an empty-string payload to clear.
            # We set qos to 1 because we want to make sure we slay the
            # zombies. retain must be true because otherwise the _last_
            # retained message will linger!
            client.publish(entity,payload='',qos=1,retain=True).wait_for_publish()
    except queue.Empty:
        logging.log(5,f"MQTT: No more stored entities to clear.")
    

def add_entity(config,client,resource):
    """ Sends the Home Assistant MQTT discovery message
        and then updates the status topics.
    """
    logging.debug(f"MBTA: add resource type '{resource['type']}' with id '{resource['id']}'")

    # Construct discovery message payload using
    # defaults from the configuration. 
    if type(config['homeassistant']['entity']) == dict:
        payload = config['homeassistant']['entity'].copy()
    else:
        logging.error(f"Config: 'entity' should be a dictionary, and isn't.")
        payload = {}

    # merge in (override) anything unique to the type
    if resource['type'] in config['homeassistant'] and type(config['homeassistant'][resource['type']]) == dict:
        merge(payload,config['homeassistant'][resource['type']],strategy=Strategy.ADDITIVE)


    # and now the things that have to be from _data_
    if 'friendly_prefix' in config['homeassistant']:
        prefix = config['homeassistant']['friendly_prefix']
    else:
        prefix = ''
    match resource['type']:
        case 'line' :
            # for whatever reason, lines already have their name in the id (but with a '-')
            payload['name']=f"{prefix}{resource['id'].replace('-',' ').title()}"
            payload['unique_id']=f"{config['homeassistant']['node_id']}_{resource['id']}"
        case 'prediction'| 'schedule':
            # predictions too. But we want to use the route for the name!
            prediction_route = resource['relationships']['route']['data']['id']
            payload['name']=f"{prediction_route}"
            # no "['resource_type']":
            payload['unique_id']=f"{config['homeassistant']['node_id']}_{resource['id']}"
        case 'stop':
            # It's nice to put bus numbers in the name, but ugly with 'place-davis'.
            # Also, we are told if it's a stop or station -- unfortunately, by hard-coded
            # numbers in the API.
            if 'location_type' in resource['attributes'] and 'location_type' in config['mbta']:
                try:
                    location_type=config['mbta']['location_type'][resource['attributes']['location_type']]
                except KeyError:
                    location_type="Unknown"
                    logging.warning(f"MBTA: Got an unknown location type in '{resource['type']} {resource['id']}' ('{resource['attributes']['location_type']}').")
            else:
                location_type=""
            if resource['id'].isnumeric():
                payload['name'] = f"{prefix}{location_type} {resource['id']} ({resource['attributes']['name']})"
            else:
                payload['name'] = f"{prefix}{resource['attributes']['name']} {location_type}"

            # same as generic
            payload['unique_id']=f"{config['homeassistant']['node_id']}_{resource['type']}_{resource['id']}"
        case _:
            payload['name']=f"{prefix}{resource['type'].replace('_',' ').capitalize()} {resource['id']}"
            payload['unique_id']=f"{config['homeassistant']['node_id']}_{resource['type']}_{resource['id']}"
    
    # apparently commuter rail service ids can contain spaces.
    # maybe other thigns too, so... make them underscores to be safe.
    payload['object_id']=payload['unique_id'].replace(' ','_')
    
    payload['availability_topic']=f"{config['mqtt']['prefix']}/status"

    payload['state_topic']=f"{config['mqtt']['prefix']}/{resource['type']}/{resource['id']}/state"
    payload['json_attributes_topic']=f"{config['mqtt']['prefix']}/{resource['type']}/{resource['id']}/attributes"

    # Predictions are a associated with stops.
    # For conveniences, we tie them together by making
    # a "device" in Home Assistant. That's done simply
    # by configuring each entity to have the same device
    # info here.
    # TODO: config-file options for individual stop ids
    if type(config['homeassistant']['device']) == dict:
        match resource['type']:
            case 'stop':
                stop_id = resource['id']
                logging.debug(f"HA: Associating Stop {stop_id} with a device.")
            case 'prediction':
                stop_id = resource['relationships']['stop']['data']['id']
                logging.debug(f"HA: Associating Prediction {resource['id']} with the device for Stop {stop_id}.")
            case _:
                stop_id = ""
        if stop_id in config['mbta']['stops']:
            payload['device'] = config['homeassistant']['device'].copy()
            payload['device']['identifiers'] =  f"mbta stop {stop_id}"
            if resource['type'] == 'stop':
                # It's nice to put bus numbers in the name, but ugly with 'place-davis'.
                # Also, we can tell if it's a stop or station -- unfortunately, by hard-coded
                # numbers in the API.
                if 'location_type' in resource['attributes'] and 'location_type' in config['mbta']:
                    try:
                        location_type=config['mbta']['location_type'][resource['attributes']['location_type']]
                    except KeyError:
                        location_type="Unknown"
                        logging.warning(f"MBTA: Got an unknown location type in '{resource['type']} {resource['id']}' ('{resource['attributes']['location_type']}').")
                else:
                    location_type=""
                if stop_id.isnumeric():
                    payload['device']['name'] = f"{prefix}{location_type} {stop_id} ({resource['attributes']['name']})"
                else:
                    payload['device']['name'] = f"{prefix}{resource['attributes']['name']} {location_type}"
    else:
        logging.debug(f"Config: 'device' is not a dictionary, so not creating devices.")

    # Allow unique configuration by id
    # (like if you want your favorite bus to be different somehow)
    try:
        merge(payload,config['homeassistant']['individual'][resource['type']][resource['id']],strategy=Strategy.ADDITIVE)
        logging.debug(f"HA: Using individual entity config for {[resource['type']]}:{[resource['id']]}")
    except KeyError:
        # it's fine. we don't need to do anything.
        pass


    topic = f"{config['homeassistant']['discovery_prefix']}/sensor/{config['homeassistant']['node_id']}/{payload['object_id']}/config"

    # todo: make qos configurable
    logging.debug(f"MQTT: Sending discovery message for '{payload['name']}'")
    logging.log(5,f"MQTT: Discovery topic for '{resource['type']} {resource['id']}' is {topic}")
    logging.log(5,f"MQTT: Discovery payload for '{resource['type']} {resource['id']}' is {payload}")
    client.publish(topic,payload=json.dumps(payload),qos=1,retain=True).wait_for_publish()
    

    # and then update the and attributes
    update_entity(config,client,resource)
    
def update_entity(config,client,resource):
    """Update state and attribute topics."""

    logging.debug(f"MBTA: update resource type '{resource['type']}' with id '{resource['id']}'")

    # We're doing attributes before state,
    # because we are going to set the state
    # based on some attribute.

    # The basic stuff we want is in the 'attributes' map
    payload=resource['attributes'].copy()

    # For some reason, Home Assistant really does not like
    # an attribute named... "name". So, map these:
    if 'name' in payload:
        payload[f"{resource['type']}_name"] = payload['name']
        if 'long_name' not in payload:
            payload['long_name'] = payload['name']    

    # These are hard-coded in the API and it makes me sad.
    if 'vehicle_types' in config['mbta']:
        target=''
        if 'vehicle_type' in payload:
            target='vehicle_type'
            index=payload['vehicle_type']
        elif 'route_type' in payload:
            target='route_type'
            index=payload['route_type']
        elif resource['type']=='route' and 'type' in payload:
            # adding "route_type" and leaving numeric 'type'. right choice? not sure!
            target='route_type'
            index=payload['type']
        if target:
            try:
                payload[target] = config['mbta']['vehicle_types'][index]
            except KeyError:
                logging.debug(f"Config: No mapping for vehicle type {index}")

    # These are also hard-coded in the API
    if resource['type']=='route_pattern' and 'typicality' in payload and 'route_pattern_typicality' in config['mbta']:
        try:
            payload['typicality_desc'] = config['mbta']['route_pattern_typicality'][payload['typicality']]
        except KeyError:
            logging.debug(f"Config: No mapping for route typicality {payload['typicality']}")
    if resource['type']=='stop' and 'location_type' in payload and 'location_type' in config['mbta']:
        try:
            payload['location_type'] = config['mbta']['location_type'][payload['location_type']]
        except KeyError:
            logging.debug(f"Config: No mapping for route location_type {payload['location_type']}")

    # There are also these 'relationships',
    # and the MBTA structure for them is kind of silly.
    # So, this kind of flattens that, for easier use...
    if 'relationships' in resource:
        for (relation, relationdata) in resource['relationships'].items():
            if 'data' in relationdata:
                if relationdata['data']:  # skip null and empty:
                    if type(relationdata['data']) is dict:
                        payload[f"{relation}_id"] = relationdata['data']['id']
                    elif type(relationdata['data']) is list:
                        payload[f"{relation}_list"] = [x['id'] for x in relationdata['data']]
                    else:
                        logging.warning(f"MBTA: Got relationship data that is neither a list nor a dict! '{resource['type']} {resource['id']}' ('{relationdata}').")

            elif 'links' in relationdata:
                payload[f"{relation}_link"] = f"{config['mbta']['server']}{relationdata['links']['related']}"
            else:
                logging.warning(f"MBTA: Got an unknown relationship in '{resource['type']} {resource['id']}' ('{relationdata}').")

    topic = f"{config['mqtt']['prefix']}/{resource['type']}/{resource['id']}/attributes"
    logging.log(5,f"MQTT: Attributes for '{resource['type']} {resource['id']}': {payload}")
    logging.debug(f"MQTT: Sending attribute message for '{resource['type']} {resource['id']}'")
    client.publish(topic,payload=json.dumps(payload),qos=1,retain=True)

    # Ok, now state:

    match resource['type']:
        case 'alert':
            state=payload['service_effect']
        case 'facility' | 'line' | 'route' | 'stop':
            state=payload['long_name']
        case 'prediction' | 'schedule':
            if payload['departure_time']:
                state=payload['departure_time']
            elif payload['arrival_time']:
                state=payload['arrival_time']
            else:
                state='unknown'
        case 'route_pattern':
            if 'time_desc' in payload and payload['time_desc']:
                state=payload['time_desc']
            elif 'typicality_desc' in payload:
                state=payload['typicality_desc']
            else:
                state=payload['long_name']
        case 'service':
            state=f"{payload['description']} ({payload['rating_description']})"
        case 'shape':
            state='polyline'
        case 'trip':
            state=payload['headsign']
        case 'vehicle':
            state=payload['current_status']
        case _:
            state='see attributes'

    topic = f"{config['mqtt']['prefix']}/{resource['type']}/{resource['id']}/state"
    logging.log(5,f"MQTT: State for '{resource['type']} {resource['id']}': {state}")
    logging.debug(f"MQTT: Sending state message for '{resource['type']} {resource['id']}'")
    client.publish(topic,payload=state,qos=1,retain=True)


def remove_entity(config,client,resource):

    logging.debug(f"MBTA: remove resource type '{resource['type']}' with id '{resource['id']}'")

    object_id = f"{config['homeassistant']['node_id']}_{resource['type']}_{resource['id']}"
    topic = f"{config['homeassistant']['discovery_prefix']}/sensor/{config['homeassistant']['node_id']}/{object_id}/config"
    
    logging.debug(f"MQTT: Sending remove message for '{resource['type']} {resource['id']}'")
    client.publish(topic,payload='',qos=1,retain=True)
    
    




if __name__ == "__main__":
    main()
