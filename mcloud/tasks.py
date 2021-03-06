import inspect
import random
import string
import subprocess
import uuid
from shutil import rmtree
from mcloud.container import PrebuiltImageBuilder

from mcloud.service import Service

from mcloud.sync import VolumeNotFound
from mcloud.util import TxTimeoutEception
import os


import re
from autobahn.twisted.util import sleep
import inject
from twisted.internet import defer, reactor
from twisted.internet.defer import inlineCallbacks
from twisted.internet.error import ConnectionDone
import txredisapi
from mcloud.txdocker import IDockerClient, NotFound


from mcloud.application import ApplicationController, AppDoesNotExist
from mcloud.deployment import DeploymentController
from mcloud.events import EventBus
from mcloud.remote import ApiRpcServer

from twisted.internet import protocol


class TicketScopeProcess(protocol.ProcessProtocol):
    def __init__(self, ticket_id, client):
        self.ticket_id = ticket_id
        self.client = client
        self.d = None

    def call_sync(self, *args, **kwargs):
        self.d = defer.Deferred()
        reactor.spawnProcess(self, *args, **kwargs)

        return self.d

    def log(self, data):
        self.client.task_log(self.ticket_id, data)

    def connectionMade(self):
        self.transport.closeStdin()

    def outReceived(self, data):
        self.log(data)

    def errReceived(self, data):
        self.log(data)

    def processExited(self, reason):
        pass
        # self.log("processExited, status %d\n" % (reason.value.exitCode,))

    def processEnded(self, reason):
        # pass
        if reason.value.exitCode != 0:
            self.log("processEnded, status %d\n" % (reason.value.exitCode,))
        self.d.callback(True)


class TaskService(object):
    app_controller = inject.attr(ApplicationController)
    """
    @type app_controller: ApplicationController
    """
    deployment_controller = inject.attr(DeploymentController)
    redis = inject.attr(txredisapi.Connection)
    rpc_server = inject.attr(ApiRpcServer)
    event_bus = inject.attr(EventBus)
    """ @type: EventBus """

    settings = inject.attr('settings')

    def task_log(self, ticket_id, message):
        message += '\n'
        self.rpc_server.task_progress(message, ticket_id)

    def task_help(self, ticket_id):
        pass

    @inlineCallbacks
    def task_init(self, ticket_id, name, path=None, config=None, env=None, deployment=None):
        """
        Initialize new application

        :param ticket_id:
        :param name: Application name
        :param path: Path to the application
        :return:
        """

        if not deployment:
            Exception('Deployment name is required!')

        app = None
        try:
            app = yield self.app_controller.get(name)
        except AppDoesNotExist:
            pass

        if app:
            raise ValueError('Application already exist')

        if not config:
            raise ValueError('config must be provided to create an application')

        if not path:
            path = '/root/.mcloud/%s' % name

        yield self.app_controller.create(name, {'path': path, 'source': config, 'env': env, 'deployment': deployment})

        defer.returnValue(True)

    @inlineCallbacks
    def task_update(self, ticket_id, name, config=None, env=None):
        """
        Initialize new application

        :param ticket_id:
        :param name: Application name
        :param path: Path to the application
        :return:
        """

        yield self.app_controller.update_source(name, source=config, env=env)

        ret = yield self.app_controller.list()
        defer.returnValue(ret)

    @inlineCallbacks
    def task_list(self, ticket_id):
        """
        List all application and data related

        :param ticket_id:
        :return:
        """
        alist = yield self.app_controller.list()
        defer.returnValue(alist)

    @inlineCallbacks
    def task_list_volumes(self, ticket_id):
        """
        List all volumes of all applications

        :param ticket_id:
        :return:
        """
        alist = yield self.app_controller.volume_list()
        defer.returnValue(alist)

    @inlineCallbacks
    def task_list_vars(self, ticket_id):
        """
        List variables

        :param ticket_id:
        :return:
        """
        vlist = yield self.redis.hgetall('vars')
        defer.returnValue(vlist)

    @inlineCallbacks
    def task_set_var(self, ticket_id, name, val):
        """
        Set variable

        :param ticket_id:
        :param name:
        :param val:
        :return:
        """
        yield self.redis.hset('vars', name, val)
        defer.returnValue((yield self.task_list_vars(ticket_id)))

    @inlineCallbacks
    def task_rm_var(self, ticket_id, name):
        """
        Remove variable

        :param ticket_id:
        :param name:
        :return:
        """
        yield self.redis.hdel('vars', name)
        defer.returnValue((yield self.task_list_vars(ticket_id)))

    @inlineCallbacks
    def task_remove(self, ticket_id, name):
        """
        Remove application

        :param ticket_id:
        :param name:
        :return:
        """
        yield self.task_destroy(ticket_id, name, scrub_data=True)
        yield self.app_controller.remove(name)

        # ret = yield self.app_controller.list()
        ret = 'Done.'
        defer.returnValue(ret)

    @inlineCallbacks
    def task_set_deployment(self, ticket_id, app, deployment):
        """
        Remove application

        :param ticket_id:
        :param name:
        :return:
        """
        yield self.app_controller.update(app, {'deployment': deployment})

        ret = 'Done.'
        defer.returnValue(ret)


    @inlineCallbacks
    def task_logs(self, ticket_id, ref):
        """
        Read logs.

        Logs are streamed as task output.

        :param ticket_id:
        :param name:
        :return:
        """

        service, app = ref.split('.')

        # todo: fix to remote client

        def on_log(log):

            if len(log) == 8 and log[7] != 0x0a:
                return

            self.task_log(ticket_id, log)

        try:
            app = yield self.app_controller.get(app)

            config = yield app.load()

            service = config.get_service(ref)
            yield service.client.logs(service.name, on_log, tail=100)
        except NotFound:
            self.task_log(ticket_id, 'Container not found by name.')


    @inlineCallbacks
    def task_run(self, ticket_id, name, command, size=None):
        """
        Run command in container.

        TaskIO is attached to container.

        :param ticket_id:
        :param name:
        :param command:
        :param size:
        :return:
        """

        service_name, app_name = name.split('.')

        try:
            app = yield self.app_controller.get(app_name)

            config = yield app.load()

            service = config.get_service('%s.%s' % (service_name, app_name))

            yield service.run(ticket_id, command, size=size)

        except NotFound:
            self.task_log(ticket_id, 'Container not found by name.')

        except ConnectionDone:
            pass


    @inlineCallbacks
    def task_config(self, ticket_id, name):
        """
        Show application detailed status

        :param ticket_id:
        :param name:
        :return:
        """
        app = yield self.app_controller.get(name)
        config = yield app.load()

        defer.returnValue({
            'path': app.config['path'],
            'env': app.get_env(),
            'source': app.config['source'] if 'source' in app.config else {},
            'hosts': config.hosts,
            'volumes': config.get_volumes() if hasattr(config, 'get_volumes') else {},
        })


    @inlineCallbacks
    def task_status(self, ticket_id, name):
        """
        Show application detailed status

        :param ticket_id:
        :param name:
        :return:
        """
        app = yield self.app_controller.get(name)
        config = yield app.load()

        """
        @type config: YamlConfig
        """

        data = []
        for service in config.get_services().values():
            """
            @type service: Service
            """

            assert service.is_inspected()

            data.append([
                service.name,
                service.is_running(),
                service.is_running()
            ])

        defer.returnValue(data)

    def sleep(self, sec):
        d = defer.Deferred()
        reactor.callLater(sec, d.callback, None)
        return d


    @inlineCallbacks
    def task_restart(self, ticket_id, name):
        """
        Restart application or services

        :param ticket_id:
        :param name:
        :return:
        """

        yield self.task_stop(ticket_id, name)
        ret = yield self.task_start(ticket_id, name)

        defer.returnValue(ret)

    @inlineCallbacks
    def task_rebuild(self, ticket_id, name, scrub_data=False):
        """
        Rebuild application or service.

        :param ticket_id:
        :param name:
        :return:
        """
        yield self.task_destroy(ticket_id, name, scrub_data=scrub_data)
        ret = yield self.task_start(ticket_id, name)

        defer.returnValue(ret)


    def follow_logs(self, service, ticket_id):

        def on_log(log):

            if log.startswith('@mcloud ready in '):
                parts = log.split(' ')
                self.event_bus.fire_event('api.%s.%s' % (service.name, 'ready'), my_args=parts[2:])
                return

            if len(log) == 8 and log[7] != 0x0a:
                return

            self.task_log(ticket_id, log)

        def done(result):
            pass

        def on_err(failure):
            print failure

        d = service.client.logs(service.name, on_log)
        d.addCallback(done)
        d.addErrback(on_err)

        self.event_bus.once('task.failure.%s' % ticket_id, d.cancel)

        return d


    @inlineCallbacks
    def task_sync_stop(self, ticket_id, app_name, sync_ticket_id):

        app = yield self.app_controller.get(app_name)

        client = yield app.get_client()

        s = Service(client=client)
        s.app_name = app_name
        s.name = '%s_%s_%s' % (app_name, '_rsync_', sync_ticket_id)

        yield s.inspect()

        if s.is_running():
            self.task_log(ticket_id, 'Stopping rsync container.')
            yield s.stop(ticket_id)

        if s.is_created():
            self.task_log(ticket_id, 'Destroying rsync container.')
            yield s.destroy(ticket_id)


    @inlineCallbacks
    def task_sync(self, ticket_id, app_name, service_name, volume):

        app = yield self.app_controller.get(app_name)

        config = yield app.load()
        client = yield app.get_client()

        s = Service(client=client)
        s.app_name = app_name
        s.name = '%s_%s_%s' % (app_name, '_rsync_', ticket_id)
        s.image_builder = PrebuiltImageBuilder(image='modera/rsync')
        s.ports = [873]

        if service_name:

            if not volume:
                raise VolumeNotFound('In case of service name is provided, volume name is mandatory!')

            services = config.get_services()

            service_full_name = '%s.%s' % (service_name, app_name)
            try:
                service = services[service_full_name]

                all_volumes = service.list_volumes()
                if not volume in all_volumes:
                    raise VolumeNotFound('Volume with name %s no found!' % volume)

                volume_name = volume
                s.volumes_from = service_full_name

            except KeyError:
                raise VolumeNotFound('Service with name %s was not found!' % service_name)

        else:
            s.volumes = [{
                             'local': app.config['path'],
                             'remote': '/volume'
                         }]
            volume_name = '/volume'

        s.env = {
            'USERNAME': ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(32)),
            'PASSWORD': ''.join(
                random.choice(string.ascii_lowercase + string.punctuation + string.digits) for _ in range(32)),
            'ALLOW': '*'
        }

        yield s.start(ticket_id)

        deployment = yield app.get_deployment()

        if deployment.local:
            sync_host = 'me'
        else:
            sync_host = deployment.host

        print s.public_ports()
        defer.returnValue({
            'env': s.env,
            'container': s.name,
            'host': sync_host,
            'port': s.public_ports()['873/tcp'][0]['HostPort'],
            'volume': volume_name,
            'ticket_id': ticket_id
        })


    @inlineCallbacks
    def task_backup(self, ticket_id, app_name, service_name, volume, destination, restore=False):

        app = yield self.app_controller.get(app_name)

        config = yield app.load()

        service = None

        if service_name:

            if not volume:
                raise VolumeNotFound('In case of service name is provided, volume name is mandatory!')

            services = config.get_services()

            service_full_name = '%s.%s' % (service_name, app_name)
            try:
                service = services[service_full_name]

                all_volumes = service.list_volumes()

                volume_path = all_volumes[volume]

            except KeyError:
                raise VolumeNotFound('Service with name %s was not found!' % service_name)

        else:
            volume_path = app.config['path']

        if not restore:

            if self.settings.btrfs:
                uuid_ = uuid.uuid1()

                snapshot_path = '%s/snapshots_%s' % (self.settings.home_dir, uuid_)

                self.task_log(ticket_id, snapshot_path)

                yield TicketScopeProcess(ticket_id, self).call_sync(
                    '/sbin/btrfs', ['btrfs', 'subvolume', 'snapshot', '-r', volume_path, snapshot_path]
                )
                self.task_log(ticket_id, '-----------------')

                volume_path = snapshot_path

            else:
                if service:
                    service.pause()

            yield TicketScopeProcess(ticket_id, self).call_sync(
                'aws', ['aws', 's3', 'sync', volume_path, destination]
            )

            if not self.settings.btrfs:
                service.unpause()
            else:
                yield TicketScopeProcess(ticket_id, self).call_sync(
                    '/sbin/btrfs', ['btrfs', 'subvolume', 'delete', volume_path]
                )

        else:
            yield TicketScopeProcess(ticket_id, self).call_sync(
                'aws', ['aws', 's3', 'sync', destination, volume_path]
            )

        defer.returnValue({
            'status': 'ok',
            'path': volume_path
        })


    @inlineCallbacks
    def task_start(self, ticket_id, name):
        """
        Start application or service.

        :param ticket_id:
        :param name:
        :return:
        """

        self.task_log(ticket_id, '[%s] Starting application' % (ticket_id, ))

        if '.' in name:
            service_name, app_name = name.split('.')
        else:
            service_name = None
            app_name = name

        app = yield self.app_controller.get(app_name)
        config = yield app.load()

        """
        @type config: YamlConfig
        """

        self.task_log(ticket_id, '[%s] Got response' % (ticket_id, ))

        for service in config.get_services().values():
            if service_name and '%s.%s' % (service_name, app_name) != service.name:
                continue

            if not service.is_created():
                self.task_log(ticket_id,
                              '[%s] Service %s is not created. Creating' % (ticket_id, service.name))
                yield service.create(ticket_id)

        for service in config.get_services().values():

            if service_name and '%s.%s' % (service_name, app_name) != service.name:
                continue

            self.task_log(ticket_id, '\n' + '*' * 50)
            self.task_log(ticket_id, '\n Service %s' % service.name)
            self.task_log(ticket_id, '\n' + '*' * 50)

            if not service.is_running():
                self.task_log(ticket_id,
                              '[%s] Service %s is not running. Starting' % (ticket_id, service.name))
                yield service.start(ticket_id)

                self.task_log(ticket_id, 'Updating container list')

                if not service.wait is False:

                    wait = service.wait
                    if wait <= 0:
                        wait = 0.2
                    if wait > 3600:
                        self.task_log(ticket_id, 'WARN: wait is to high, forcibly set to 3600s to prevent memory leaks')
                        wait = 3600

                    log_process = self.follow_logs(service, ticket_id)

                    self.task_log(ticket_id, 'Waiting for container to start. %s' % (
                        'without timeout' if wait == 0 else 'with timout %ss' % wait))

                    try:
                        event = yield self.event_bus.wait_for_event('api.%s.*' % service.name, wait)
                    except TxTimeoutEception:
                        event = None

                    timeout_happenned = event is None

                    if timeout_happenned:
                        self.task_log(ticket_id, '%s seconds passed.' % wait)
                        yield service.inspect()

                        if not service.is_running():
                            self.task_log(ticket_id,
                                          'FATAL: Service is not running after timeout. Stopping application execution.')
                            log_process.cancel()
                            defer.returnValue(False)
                        else:
                            self.task_log(ticket_id, 'Container still up. Continue execution.')
                    else:
                        sleep_time = 0.5
                        if 'my_args' in event and len(event['my_args']) == 2:
                            if event['my_args'][0] == 'in':
                                match = re.match('^([0-9]+)s$', event['my_args'][1])
                                if match:
                                    sleep_time = float(match.group(1))

                        self.task_log(ticket_id,
                                      'Container is waiting %ss to make sure container is started.' % sleep_time)
                        yield sleep(sleep_time)

                        if not service.is_running():
                            self.task_log(ticket_id,
                                          'FATAL: Service is not running after ready report. Stopping application execution.')
                            log_process.cancel()
                            defer.returnValue(False)
                        else:
                            self.task_log(ticket_id, 'Container still up. Continue execution.')

                    log_process.cancel()

                else:
                    yield sleep(0.2)

                self.event_bus.fire_event('containers-updated')

            else:
                self.task_log(ticket_id,
                              '[%s] Service %s is already running.' % (ticket_id, service.name))

        # ret = yield self.app_controller.list()
        ret = 'Done.'
        defer.returnValue(ret)


    @inlineCallbacks
    def task_create(self, ticket_id, name):

        """
        Create application containers without starting.

        :param ticket_id:
        :param name:
        :return:
        """

        self.task_log(ticket_id, '[%s] Creating application' % (ticket_id, ))

        if '.' in name:
            service_name, app_name = name.split('.')
        else:
            service_name = None
            app_name = name

        app = yield self.app_controller.get(app_name)
        config = yield app.load()

        """
        @type config: YamlConfig
        """

        self.task_log(ticket_id, '[%s] Got response' % (ticket_id, ))

        for service in config.get_services().values():
            if service_name and '%s.%s' % (service_name, app_name) != service.name:
                continue

            if not service.is_created():
                self.task_log(ticket_id,
                              '[%s] Service %s is not created. Creating' % (ticket_id, service.name))
                yield service.create(ticket_id)

        # ret = yield self.app_controller.list()
        ret = 'Done.'
        defer.returnValue(ret)


    @inlineCallbacks
    def task_stop(self, ticket_id, name):
        """
        Stop application containers without starting.

        :param ticket_id:
        :param name:
        :return:
        """

        self.task_log(ticket_id, '[%s] Stoping application' % (ticket_id, ))

        if '.' in name:
            service_name, app_name = name.split('.')
        else:
            service_name = None
            app_name = name

        app = yield self.app_controller.get(app_name)
        config = yield app.load()

        """
        @type config: YamlConfig
        """

        self.task_log(ticket_id, '[%s] Got response' % (ticket_id, ))

        d = []
        for service in config.get_services().values():

            if service_name and '%s.%s' % (service_name, app_name) != service.name:
                continue

            if service.is_running():
                self.task_log(ticket_id,
                              '[%s] Service %s is running. Stoping' % (ticket_id, service.name))
                d.append(service.stop(ticket_id))
            else:
                self.task_log(ticket_id,
                              '[%s] Service %s is already stopped.' % (ticket_id, service.name))

        yield defer.gatherResults(d)

        # ret = yield self.app_controller.list()
        ret = 'Done.'
        defer.returnValue(ret)

    @inlineCallbacks
    def task_destroy(self, ticket_id, name, scrub_data=False):

        """
        Remove application containers.

        :param ticket_id:
        :param name:
        :return:
        """

        self.task_log(ticket_id, '[%s] Destroying application containers' % (ticket_id, ))

        if '.' in name:
            service_name, app_name = name.split('.')
        else:
            service_name = None
            app_name = name

        app = yield self.app_controller.get(app_name)
        config = yield app.load()

        """
        @type config: YamlConfig
        """

        self.task_log(ticket_id, '[%s] Got response' % (ticket_id, ))

        if isinstance(config, dict):
            self.task_log(ticket_id, 'Application location does not exist, use remove command to remove application')
            self.task_log(ticket_id, config['message'])
            return

        d = []
        for service in config.get_services().values():

            if service_name and '%s.%s' % (service_name, app_name) != service.name:
                continue

            self.task_log(ticket_id, '[%s] Destroying container: %s' % (ticket_id, service_name))

            if service.is_created():
                if service.is_running():
                    self.task_log(ticket_id,
                                  '[%s] Service %s container is running. Stopping and then destroying' % (
                                      ticket_id, service.name))
                    yield service.stop(ticket_id)
                    d.append(service.destroy(ticket_id))

                else:
                    self.task_log(ticket_id,
                                  '[%s] Service %s container is created. Destroying' % (ticket_id, service.name))
                    d.append(service.destroy(ticket_id))
            else:
                self.task_log(ticket_id,
                              '[%s] Service %s container is not yet created.' % (ticket_id, service.name))

            if scrub_data:
                self.task_log(ticket_id, '[%s] Scrubbing container data: %s' % (ticket_id, service.name))
                dir_ = os.path.expanduser('%s/volumes/%s' % (self.settings.home_dir, service.name))
                if os.path.exists(dir_):
                    rmtree(dir_)
                    self.task_log(ticket_id, '[%s] Removed dir: %s' % (ticket_id, dir_))
                else:
                    self.task_log(ticket_id, '[%s] Nothing to remove' % ticket_id)


        yield defer.gatherResults(d)

        # ret = yield self.app_controller.list()
        ret = 'Done.'
        defer.returnValue(ret)


    @inlineCallbacks
    def task_inspect(self, ticket_id, name, service_name):
        """
        Get inspect data of service

        :param ticket_id:
        :param name:
        :param service_name:
        :return:
        """

        self.task_log(ticket_id, '[%s] Inspecting application service %s' %
                      (ticket_id, service_name))

        app = yield self.app_controller.get(name)
        config = yield app.load()

        """
        @type config: YamlConfig
        """
        self.task_log(ticket_id, '[%s] Got response' % (ticket_id, ))

        service = config.get_service('%s.%s' % (service_name, name))
        if not service.is_created():
            defer.returnValue('Not created')
        else:
            if not service.is_inspected():
                ret = yield service.inspect()
                defer.returnValue(ret)
            else:
                defer.returnValue(service._inspect_data)


    @inlineCallbacks
    def task_deployments(self, ticket_id):
        """
        List deployments (published URLs)

        :param ticket_id:
        :return:
        """

        deployments = yield self.deployment_controller.list()

        deployment_list = []

        for deployment in deployments:
            deployment_list.append(deployment.load_data())

        ret = yield defer.gatherResults(deployment_list, consumeErrors=True)
        defer.returnValue(ret)


    @inlineCallbacks
    def task_machine(self, ticket_id, command):
        """
        Execute command through docker-machine

        :param ticket_id:
        :return:
        """
        vlist = yield self.redis.hgetall('vars')

        command = ['docker-machine'] + command

        yield TicketScopeProcess(ticket_id, self).call_sync(
            '/usr/local/bin/docker-machine', command, env=vlist
        )

        yield self.deployment_controller.configure_docker_machine()


    @inlineCallbacks
    def task_deployment_info(self, ticket_id, name=None):
        if name:
            deployment = yield self.deployment_controller.get(name=name)
        else:
            deployment = yield self.deployment_controller.get_default()

        if not deployment:
            defer.returnValue(None)

        data = yield deployment.load_data()

        defer.returnValue(data)


    @inlineCallbacks
    def task_app_deployment_info(self, ticket_id, name=None):
        app = yield self.app_controller.get(name=name)
        deployment = yield app.get_deployment()

        if not deployment:
            defer.returnValue(None)

        data = yield deployment.load_data()

        defer.returnValue(data)

    @inlineCallbacks
    def task_deployment_create(self, ticket_id, **kwargs):
        deployment = yield self.deployment_controller.create(**kwargs)
        deployments = yield self.deployment_controller.list()
        if len(deployments) == 1:
            yield self.deployment_controller.set_default(deployment.name)
            
        defer.returnValue(not deployment is None)

    @inlineCallbacks
    def task_deployment_set_default(self, ticket_id, name):
        yield self.deployment_controller.set_default(name)

    @inlineCallbacks
    def task_deployment_update(self, ticket_id, **kwargs):
        deployment = yield self.deployment_controller.update(**kwargs)
        defer.returnValue(not deployment is None)

    @inlineCallbacks
    def task_deployment_remove(self, ticket_id, name):
        deployment = yield self.deployment_controller.remove(name)
        defer.returnValue(deployment is None)

    @inlineCallbacks
    def task_publish(self, ticket_id, domain_name, app_name, service_name, custom_port=None):
        """
        Publish application URL.

        :param ticket_id:
        :param deployment_name:
        :param app_name:
        :return:
        """
        app = yield self.app_controller.get(app_name)
        deployment = yield app.get_deployment()

        yield self.deployment_controller.publish_app(deployment, domain_name, app_name, service_name, custom_port, ticket_id=ticket_id)

        ret = yield self.app_controller.list()
        defer.returnValue(ret)

    @inlineCallbacks
    def task_unpublish(self, ticket_id, domain_name, app_name):
        """
        Unpublish URL

        :param ticket_id:
        :param deployment_name:
        :return:
        """
        app = yield self.app_controller.get(app_name)
        deployment = yield app.get_deployment()

        yield self.deployment_controller.unpublish_app(deployment, domain_name, ticket_id=ticket_id)

        ret = yield self.app_controller.list()
        defer.returnValue(ret)

    def collect_tasks(self, ):

        tasks = {}
        for name, func in inspect.getmembers(self):
            if name.startswith('task_'):
                tasks[name[5:]] = func

        return tasks

