from collections import defaultdict
import json
import logging
import inject
from mfcloud.config import YamlConfig, ConfigParseError
import os
from twisted.internet import defer, reactor
from twisted.internet.defer import inlineCallbacks
import txredisapi


class Application(object):

    dns_search_suffix = inject.attr('dns-search-suffix')
    #host_ip = inject.attr('host_ip')

    def __init__(self, config, name=None, public_url=None):
        super(Application, self).__init__()

        self.config = config
        self.name = name
        self.public_url = public_url

    @defer.inlineCallbacks
    def load(self, need_details=False):

        try:
            if 'path' in self.config:
                yaml_config = YamlConfig(file=os.path.join(self.config['path'], 'mfcloud.yml'), app_name=self.name)
            elif 'source' in self.config:
                yaml_config = YamlConfig(source=self.config['source'], app_name=self.name)
            else:
                raise ConfigParseError('Can not load config.')

            yaml_config.load()

            yield defer.gatherResults([service.inspect() for service in yaml_config.get_services().values()])

            if need_details:
                defer.returnValue(self._details(yaml_config))
            else:
                defer.returnValue(yaml_config)

        except ValueError as e:
            defer.returnValue({
                'name': self.name,
                'config': self.config,
                #'host_ip': self.host_ip,
                'services': [],
                'running': False,
                'status': 'error',
                'message': '%s When loading config: %s' % (e.message, self.config)
            })



    def _details(self, app_config):
        is_running = True
        status = 'RUNNING'

        web_ip = None
        web_service = None

        services = []
        for service in app_config.get_services().values():
            services.append({
                'name': service.name,
                'ip': service.ip(),
                'ports': service.public_ports(),
                'volumes': service.attached_volumes(),
                'started_at': service.started_at(),
                'fullname': '%s.%s' % (service.name, self.dns_search_suffix),
                'is_web': service.is_web(),
                'running': service.is_running(),
                'created': service.is_created(),
            })

            if service.is_web():
                web_ip = service.ip()
                web_service = service.name

            if not service.is_running():
                is_running = False
                status = 'STOPPED'

        return {
            'name': self.name,
            'fullname': '%s.%s' % (self.name, self.dns_search_suffix),
            'web_ip': web_ip,
            'web_service': web_service,
            'public_url': self.public_url,
            'config': self.config,
            'services': services,
            'running': is_running,
            'status': status
        }


class AppDoesNotExist(Exception):
    pass


class ApplicationController(object):

    redis = inject.attr(txredisapi.Connection)

    @defer.inlineCallbacks
    def create(self, name, config, skip_validation=False):

        # validate first by crating application instance
        if not skip_validation:
            ret = yield Application(config).load()

        #  set data to redis. we don't care too much about result
        ret = yield self.redis.hset('mfcloud-apps', name, json.dumps(config))
        defer.returnValue(Application(config, name=name))

    @defer.inlineCallbacks
    def update(self, name, config):

        data = yield self.redis.hget('mfcloud-apps', name)

        data.update(config)
        ret = yield self.redis.hset('mfcloud-apps', name, json.dumps(data))

        defer.returnValue(ret)

    @defer.inlineCallbacks
    def remove(self, name):
        ret = yield self.redis.hdel('mfcloud-apps', name)
        defer.returnValue(ret)

    @defer.inlineCallbacks
    def get(self, name):
        """
        Return application instance by it's name
        """
        config = yield self.redis.hget('mfcloud-apps', name)

        if not config:
            raise AppDoesNotExist('Application with name "%s" do not exist' % name)
        else:
            defer.returnValue(Application(json.loads(config), name=name))

    @defer.inlineCallbacks
    def list(self, *args):


        deps = yield self.redis.hgetall('mfcloud-deployments')

        # collect published applications
        pub_apps = {}
        for name, config_raw in deps.items():
            try:
                dep = json.loads(config_raw)
                if 'public_app' in dep and dep['public_app']:
                    pub_apps[dep['public_app']] = dep['public_domain']
            except ValueError:
                pass


        # collect application data
        config = yield self.redis.hgetall('mfcloud-apps')

        all_apps = []
        for name, app_config in config.items():
            try:
                public_url = pub_apps[name]
            except KeyError:
                public_url = None

            app = Application(json.loads(app_config), name=name, public_url=public_url)
            all_apps.append(app.load(need_details=True))

        results = yield defer.gatherResults(all_apps, consumeErrors=True)

        defer.returnValue(results)

