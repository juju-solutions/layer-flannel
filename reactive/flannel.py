from shlex import split
from subprocess import check_call

from charms.docker import Compose
from charms.docker import DockerOpts

from charms.reactive import is_state
from charms.reactive import set_state
from charms.reactive import remove_state
from charms.reactive import when
from charms.reactive import when_any
from charms.reactive import when_not
from charms.templating.jinja2 import render
from charmhelpers.core.hookenv import config
from charmhelpers.core.hookenv import status_set
from charmhelpers.core.host import service_restart
from charmhelpers.core.host import service_stop
from charmhelpers.core import host
from charmhelpers.core import unitdata

import charms.apt
import os
import subprocess
import time


# Network Port Map
# protocol | port | source       | purpose
# ----------------------------------------------------------------------
# UDP     | 8285  | worker nodes | Flannel overlay network - UDP Backend.
# UDP     | 8472  | worker nodes | Flannel overlay network - vxlan backend


# Example subnet.env file
# FLANNEL_NETWORK=10.1.0.0/16
# FLANNEL_SUBNET=10.1.57.1/24
# FLANNEL_MTU=1450
# FLANNEL_IPMASQ=false

@when('docker.ready', 'flannel.bootstrap_daemon.available')
@when_not('etcd.connected')
def halt_execution():
    status_set('waiting', 'Waiting for etcd relation.')


@when('docker.ready')
@when_not('bootstrap_daemon.available')
def deploy_docker_bootstrap_daemon():
    ''' This is a nifty trick. We're going to init and start
    a secondary docker engine instance to run applications that
    can modify the "workload docker engine" '''
    # Render static template for init job
    status_set('maintenance', 'Configuring bootstrap docker daemon.')
    codename = host.lsb_release()['DISTRIB_CODENAME']

    # Render static template for daemon options
    render('bootstrap-docker.defaults', '/etc/default/bootstrap-docker', {},
           owner='root', group='root')

    # The templates are static, but running through the templating engine for
    # future modification. This doesn't add much overhead.
    if codename == 'trusty':
        render('bootstrap-docker.upstart', '/etc/init/bootstrap-docker.conf',
               config(), owner='root', group='root')
    else:
        # Render the service definition
        render('bootstrap-docker.service',
               '/lib/systemd/system/bootstrap-docker.service',
               config(), owner='root', group='root')
        # let systemd allocate the unix socket
        render('bootstrap-docker.socket',
               '/lib/systemd/system/bootstrap-docker.socket',
               {}, owner='root', group='root')
        # this creates the proper symlinks in /etc/systemd/system path
        check_call(split('systemctl enable /lib/systemd/system/bootstrap-docker.socket'))  # noqa
        check_call(split('systemctl enable /lib/systemd/system/bootstrap-docker.service'))  # noqa

    # start the bootstrap daemon
    service_restart('bootstrap-docker')
    set_state('bootstrap_daemon.available')


@when('bootstrap_daemon.available', 'etcd.available')
@when_not('sdn.available')
def initialize_networking_configuration(etcd):
    ''' Use an emphemeral instance of the configured ETCD container to
    initialize the CIDR range flannel can pull from. This becomes a single
    use tool.
    '''
    # Due to how subprocess mangles the JSON string, turn the hack script
    # formerly known as scripts/bootstrap.sh into this single-command
    # wrapper, under template control.
    status_set('maintenance', 'Configuring etcd keystore for flannel CIDR.')

    context = {}
    if is_state('etcd.tls.available'):
        cert_path = '/etc/ssl/flannel'
        etcd.save_client_credentials('{}/client-key.pem'.format(cert_path),
                                     '{}/client-cert.pem'.format(cert_path),
                                     '{}/client-ca.pem'.format(cert_path))
    else:
        cert_path = None

    context.update(config())
    context.update({'connection_string': etcd.get_connection_string(),
                    'socket': 'unix:///var/run/bootstrap-docker.sock',
                    'cert_path': cert_path})

    render('subnet-runner.sh', 'files/flannel/subnet.sh', context, perms=0o755)
    check_call(split('files/flannel/subnet.sh'))
    set_state('flannel.subnet.configured')


@when('flannel.subnet.configured', 'etcd.available')
@when_not('sdn.available')
def run_flannel(etcd):
    ''' Render the docker-compose template, and run the flannel daemon '''

    status_set('maintenance', 'Starting flannel network container.')
    context = {}
    if is_state('etcd.tls.available'):
        cert_path = '/etc/ssl/flannel'
    else:
        cert_path = None
    # Put all the configuration values in the context dictionary.
    context.update(config())
    iface = config('iface')
    # When iface is None or empty string.
    if not iface:
        # Attempt to detect the default interface.
        iface = get_default_interface()
        # When detection not successful, print message and return.
        if not iface:
            status_set('blocked', "Interface detection failed. "
                       "Set charm's iface config option.")
            return
    # Add additional key/values to the context dictionary.
    context.update({'charm_dir': os.getenv('CHARM_DIR'),
                    'connection_string': etcd.get_connection_string(),
                    'cert_path': cert_path})
    # Render the flannel-compose.yml file using the current context.
    render('flannel-compose.yml', 'files/flannel/docker-compose.yml', context)

    compose = Compose('files/flannel',
                      socket='unix:///var/run/bootstrap-docker.sock')
    compose.up()
    # Give the flannel daemon a moment to actually generate the interface
    # configuration seed. Otherwise we enter a time/wait scenario which
    # may cause this to be called out of order and break the expectation
    # of the deployment.
    time.sleep(3)
    ingest_network_config()


@when('flannel.configuring')
@when_not('flannel.bridge.configured')
def reconfigure_docker_for_sdn():
    ''' By default docker uses the docker0 bridge for container networking.
    This method removes the default docker bridge, and reconfigures the
    DOCKER_OPTS to use the flannel networking bridge '''

    status_set('maintenance', 'Configuring docker for flannel networking.')
    service_stop('docker')
    # cmd = "ifconfig docker0 down"
    # ifconfig doesn't always work. use native linux networking commands to
    # mark the bridge as inactive.
    cmd = "ip link set docker0 down"
    check_call(split(cmd))

    charms.apt.queue_install(['bridge-utils'])

    cmd = "brctl delbr docker0"
    check_call(split(cmd))

    set_state('docker.restart')
    remove_state('flannel.configuring')
    set_state('flannel.bridge.configured')


@when_any('config.http_proxy.changed', 'config.https_proxy.changed')
def rerender_service_template():
    ''' If we change proxy settings, re-render the bootstrap service definition
    and attempt to resume where we left off.  '''

    # Note: At this point if we hijack the workload daemon, heavy fisted
    # reprocussions will occur, like disruption  of services.

    codename = host.lsb_release()['DISTRIB_CODENAME']
    # by default, dont reboot the daemon unless we have previously rendered
    # system files.

    # Deterministic method to probe if we actually need to restart the
    # daemon.
    reboot = (os.path.exists('/lib/systemd/system/bootstrap-docker.service') or
              os.path.exists('/etc/init/bootstrap-docker.conf'))

    if codename != "trusty":
        # Handle SystemD
        render('bootstrap-docker.service',
               '/lib/systemd/system/bootstrap-docker.service',
               config(), owner='root', group='root')
        cmd = ["systemctl", "daemon-reload"]
        check_call(cmd)
    else:
        # Handle Upstart
        render('bootstrap-docker.upstart',
               '/etc/init/bootstrap-docker.conf',
               config(), owner='root', group='root')

    if reboot:
        service_restart('bootstrap-docker')


@when_any('config.cidr.changed', 'config.etcd_image.changed',
          'config.flannel_image.changed', 'config.iface.changed')
def reconfigure_flannel_network():
    ''' When the user changes the cidr, we need to reconfigure the
    backing etcd_store, and re-launch the flannel docker container.'''
    # Stop any running flannel containers
    compose = Compose('files/flannel')
    compose.kill()
    compose.rm()

    remove_state('flannel.subnet.configured')
    remove_state('flannel.bridge.configured')
    remove_state('sdn.available')


def ingest_network_config():
    ''' When flannel configures itself on first boot, it generates an
    environment file (subnet.env).

    We will parse the data we need from this and cache in unitdata so we
    can hand it off between layers, and place in the dockeropts databag
    to configure the workload docker daemon
    '''
    db = unitdata.kv()
    opts = DockerOpts()

    if not os.path.isfile('subnet.env'):
        status_set('waiting', 'No subnet file to ingest.')
        return

    with open('subnet.env') as f:
        flannel_config = f.readlines()

    for f in flannel_config:
        if "FLANNEL_SUBNET" in f:
            value = f.split('=')[-1].strip()
            db.set('sdn_subnet', value)
            opts.add('bip', value)
        if "FLANNEL_MTU" in f:
            value = f.split('=')[1].strip()
            db.set('sdn_mtu', value)
            opts.add('mtu', value)

    set_state('sdn.available')
    set_state('flannel.configuring')


def get_default_interface():
    '''Find the default network interface for this host.'''
    cmd = ['route']
    # The route command lists the default interfaces.
    # Destination    Gateway        Genmask      Flags Metric Ref    Use Iface
    # default        10.128.0.1     0.0.0.0      UG    0      0        0 ens4
    output = subprocess.check_output(cmd).decode('utf8')
    # Parse each onen of the lines.
    for line in output.split('\n'):
        # When the line contains 'default'.
        if 'default' in line:
            # The last column is the network interface.
            return line.split(' ')[-1]
