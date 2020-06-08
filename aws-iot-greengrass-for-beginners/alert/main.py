# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import argparse
import time
import logging
import os
import signal
import sys
import traceback
from datetime import datetime
from awscrt import io
from awscrt.io import LogLevel
from awscrt.mqtt import QoS, ConnectReturnCode
from awsiot.greengrass_discovery import DiscoveryClient
from awsiot import mqtt_connection_builder


APP_ROOT = os.path.dirname(os.path.abspath(__file__)) + "/"
CERT_ROOT = APP_ROOT + "certs/"
GROUP_CA_FILE = CERT_ROOT + "group_ca.pem"

private_key_path = None
certtificate_path = None
root_ca_path = None
device_name = None
region = None
mqtt_connection = None

logger = logging.getLogger()
handler = logging.StreamHandler(sys.stdout)
logger.addHandler(handler)
logger.setLevel(logging.INFO)
logging.basicConfig()


def find_cert_file(cert_prefix):
    """
    Find the certificates file from ./certs directory

    Parameters
    ----------
    cert_prefix: AmazonRootCA1.pem, cert.pem, private.key

    Returns
    ----------
    file_path: String

    """

    for _, _, names in os.walk(CERT_ROOT):
        for file in names:
            if cert_prefix in file:
                return CERT_ROOT + "/" + file

    raise Exception("%s not found." % cert_prefix)


def arg_check():
    """
    argument check
    """
    global private_key_path, certtificate_path, root_ca_path, device_name, region

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-n',
        '--thing-name',
        action='store',
        required=True,
        dest='thing_name',
        help='Targeted thing name')
    parser.add_argument(
        '--region',
        action='store',
        dest='region',
        default='ap-northeast-1')
    parser.add_argument('-v', '--verbosity', choices=[x.name for x in LogLevel], default=LogLevel.NoLogs.name,
                        help='Logging level')

    args = parser.parse_args()

    log_level = getattr(io.LogLevel, args.verbosity, "error")
    io.init_logging(log_level, 'stderr')
    loglevel_map = [
        logging.INFO, logging.INFO, logging.INFO,
        logging.INFO, logging.INFO, logging.DEBUG,
        logging.DEBUG]
    logger.setLevel(loglevel_map[log_level])
    logging.basicConfig()

    private_key_path = find_cert_file("private.key")
    certtificate_path = find_cert_file("cert.pem")
    root_ca_path = find_cert_file("AmazonRootCA1.pem")
    device_name = args.thing_name
    region = args.region


def discover_gg_host():

    event_loop_group = io.EventLoopGroup(1)
    host_resolver = io.DefaultHostResolver(event_loop_group)
    client_bootstrap = io.ClientBootstrap(event_loop_group, host_resolver)

    tls_options = io.TlsContextOptions.create_client_with_mtls_from_path(
        certtificate_path, private_key_path)
    #tls_options.override_default_trust_store_from_path(None, root_ca_path)
    tls_context = io.ClientTlsContext(tls_options)

    socket_options = io.SocketOptions()
    socket_options.connect_timeout_ms = 3000

    logger.info('Performing greengrass discovery...')
    discovery_client = DiscoveryClient(
        client_bootstrap, socket_options, tls_context, region)
    resp_future = discovery_client.discover(device_name)
    discover_response = resp_future.result()

    logger.debug(discover_response)

    for gg_group in discover_response.gg_groups:
        for gg_core in gg_group.cores:
            for connectivity_info in gg_core.connectivity:
                try:
                    print(
                        'Trying core {} at host {} port {}'.format(
                            gg_core.thing_arn,
                            connectivity_info.host_address,
                            connectivity_info.port))
                    connection = mqtt_connection_builder.mtls_from_path(
                        endpoint=connectivity_info.host_address,
                        port=connectivity_info.port,
                        cert_filepath=certtificate_path,
                        pri_key_filepath=private_key_path,
                        client_bootstrap=client_bootstrap,
                        ca_bytes=gg_group.certificate_authorities[0].encode(
                            'utf-8'),
                        on_connection_interrupted=on_connection_interupted,
                        on_connection_resumed=on_connection_resumed,
                        client_id=device_name,
                        clean_session=False,
                        keep_alive_secs=6)

                    connect_future = connection.connect()
                    connect_future.result()
                    print('Connected!')

                    return connection

                except Exception as e:
                    print('Connection failed with exception {}'.format(e))
                    continue

    sys.exit('All connection attempts failed')


def on_connection_interupted(connection, error, **kwargs):
    logger.info('connection interrupted with error {}' % error)


# Callback when an interrupted connection is re-established.
def on_connection_resumed(connection, return_code, session_present, **kwargs):
    print(
        "Connection resumed. return_code: {} session_present: {}".format(
            return_code,
            session_present))

    if return_code == ConnectReturnCode.ACCEPTED and not session_present:
        print("Session did not persist. Resubscribing to existing topics...")
        resubscribe_future, _ = connection.resubscribe_existing_topics()

        # Cannot synchronously wait for resubscribe result because we're on the connection's event-loop thread,
        # evaluate result with a callback instead.
        resubscribe_future.add_done_callback(on_resubscribe_complete)


def on_resubscribe_complete(resubscribe_future):
    resubscribe_results = resubscribe_future.result()
    print("Resubscribe results: {}".format(resubscribe_results))

    for topic, qos in resubscribe_results['topics']:
        if qos is None:
            sys.exit("Server rejected resubscribe to topic: {}".format(topic))


def on_message_received(topic, payload, **kwargs):
    print("Alert!! Received message from topic '{}': {}".format(topic, payload))


def device_main():
    """
    main loop for Alert device
    """
    global device_name, mqtt_connection

    arg_check()

    mqtt_connection = discover_gg_host()
    topic = "alert/#"
    subscribe_future, _ = mqtt_connection.subscribe(
        topic, QoS.AT_MOST_ONCE, on_message_received)
    subscribe_result = subscribe_future.result()
    print("Subscribed to {} with {}".format(
        topic, str(subscribe_result['qos'])))

    while True:
        # eait for events
        time.sleep(1)


def exit_sample(msg_or_exception):
    """
    Exit sample with cleaning

    Parameters
    ----------
    msg_or_exception: str or Exception
    """
    global mqtt_connection

    if isinstance(msg_or_exception, Exception):
        logger.error("Exiting sample due to exception.")
        traceback.print_exception(
            msg_or_exception.__class__,
            msg_or_exception,
            sys.exc_info()[2])
    else:
        logger.info("Exiting: %s", msg_or_exception)

    if not mqtt_connection:
        logger.info("Disconnecting...")
        mqtt_connection.disconnect()
    sys.exit(0)


def exit_handler(_signal, frame):
    """
    Exit sample
    """
    exit_sample(" Key abort")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, exit_handler)

    device_main()