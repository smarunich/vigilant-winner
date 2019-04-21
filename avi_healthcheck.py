#!/usr/bin/python
import requests
import re
import json
import paramiko
import kubernetes.client
import openshift.client
import argparse
import StringIO
import requests.packages.urllib3
requests.packages.urllib3.disable_warnings()


class Avi(object):
    def __init__(self, host='127.0.0.1', username='admin', password=None, verify=False, out_dir=None, tenant='*', avi_api_version='17.2.10'):
        if password == None:
            raise Exception('Avi authentication account password not provided')
        self.export = None
        self.se_connections = []
        self.ctrl_connections = []
        self.node_connections = []
        self.k8s = []
        self.host = host
        self.username = username
        self.password = password
        self.out_dir = out_dir
        self.base_url = 'https://' + host
        self.session = requests.session()
        self.session.verify = verify
        self.avi_api_version = avi_api_version
        self.tenant = tenant

        self.login()
        self.backup()
        self.alerts()
        self.vs_inventory()
        self.network_inventory()
        self.dns_metrics()
        self.cl_list = self._cluster_runtime()
        self.cc_list = self._ocp_connectors()

        for c in self.cc_list:
            self.k8s.append(K8s(k8s_cloud=c))
            for se_ip in self._se_local_addresses(cloud_uuid=c['uuid']):
                internals = self._get('/api/cloud/' + c['uuid'] + '/internals').json()
                for node in internals['agents'][0]['oshift_k8s']['hosts']:
                    user = self._find_cc_user(cloud=c)
                    self.node_connections.append(K8sNode(node['host_ip'], controllers=self.cl_list, **user))
                self.se_connections.append(AviSE(se_ip, password=self.password, controllers=self.cl_list))

        for c_ip in self.cl_list:
            self.ctrl_connections.append(AviController(c_ip, password=self.password, controllers=self.cl_list))

    def dns_metrics(self):
        self._get_dns_vs()
        uuid = self._dns_vs['uuid']
        path = '/api/analytics/metrics/virtualservice/' + uuid
        metrics = ['dns_client.avg_resp_type_a',
                   'dns_client.avg_resp_type_aaaa',
                   'dns_client.avg_resp_type_ns',
                   'dns_client.avg_resp_type_srv',
                   'dns_client.avg_resp_type_mx',
                   'dns_client.avg_resp_type_other',
                   'dns_client.avg_complete_queries',
                   'dns_client.avg_invalid_queries',
                   'dns_client.avg_domain_lookup_failures',
                   'dns_client.avg_unsupported_queries',
                   'dns_client.avg_gslbpool_member_not_available',
                   'dns_client.avg_tcp_passthrough_errors',
                   'dns_client.avg_udp_passthrough_errors',
                   'dns_client.pct_errored_queries',
                   'dns_client.avg_tcp_queries',
                   'dns_client.avg_udp_queries',
                   'l4_client.avg_bandwidth']
        m = ','.join(metrics)
        params = {'metric_id': m,
                  'aggregation': 'METRICS_ANOMALY_AGG_COUNT',
                  'aggregation_window': '1',
                  'step': '3600',
                  'limit': '168'}
        self._get(path, params=params)

    def vs_inventory(self):
        r = self._get('/api/virtualservice-inventory', params={'include_name': True})

    def network_inventory(self)
        r = self._get('/api/network-inventory', params={'include_name': True})

    def alerts(self):
        r = self._get('/api/alert')

    def backup(self):
        self.session.headers.update({'X-Avi-Tenant': 'admin'})
        r = self._post('/api/configuration/export', params={'full_system': True}, data={'passphrase': self.password})
        self.export = r.json()
        self.session.headers.update({'X-Avi-Tenant': self.tenant})

    def login(self):
        login_data = {'username': self.username, 'password': self.password}
        r = self._post('/login', data=login_data)
        if r and 'AVI_API_VERSION' in r.headers.keys():
            self.session.headers.update({'X-Avi-Version': self.avi_api_version})
            self.session.headers.update({'X-Avi-Tenant': self.tenant})
        else:
            raise Exception('Failed authenticating as %s:%s with host %s' % (self.username, self.password, self.host))

    def _find_cc_user(self, cloud=None):
        user = {}
        if cloud is not None:
            uuid = self._get(cloud['oshiftk8s_configuration']['ssh_user_ref']).json()['results'][0]['uuid']

            for ccu in self.export['CloudConnectorUser']:
                if ccu['uuid'] == uuid:
                    user['username'] = ccu['name']
                    user['pem'] = ccu['private_key']
        return user

    def _get_dns_vs(self):
        ref = self.export['SystemConfiguration'][0]['dns_virtualservice_refs'][0]
        self._dns_vs = self._get(ref, params={'include_name': True}).json()

    def _cluster_runtime(self):
        ips = []
        cluster = self._get('/api/cluster/runtime')
        for node in cluster.json()['node_states']:
            ips.append(node['mgmt_ip'])
        return ips

    def _se_local_addresses(self, cloud_uuid=None):
        se_list = []
        inventory = self._get('/api/serviceengine-inventory', params={'include_name': True})
        securechannel = self._get('/api/securechannel')
        for sc in securechannel.json()['results']:
            for se in inventory.json()['results']:
                if sc['uuid'] == se['uuid'] and cloud_uuid in se['config']['cloud_ref']:
                    se_list.append(sc['local_ip'])
        return se_list

    def _ocp_connectors(self):
        cc_list = []
        clouds = self._get('/api/cloud')
        # for cloud in clouds.json()['results']:
        for cloud in self.export['Cloud']:
            if cloud['vtype'] == 'CLOUD_OSHIFT_K8S':
                cc_list.append(cloud)
        return cc_list

    def _get(self, uri, params=None):
        url = self._url(uri)
        return self._request('get', url, params=params)

    def _post(self, uri, params=None, data=None):
        if 'login' not in uri:
            self.session.headers.update({'X-CSRFToken': self.session.cookies['csrftoken']})
            self.session.headers.update({'Referer': self.base_url + '/' })
        url = self._url(uri)
        return self._request('post', url, params=params, data=data)

    def _url(self, uri):
        return self.base_url + uri

    def _write(self, path, data):
        try:
            with open(path, 'w') as fh:
                json.dump(data, fh)
        except Exception as e:
            print e.message

    def _request(self, method, url, params=None, data=None):
        _method = getattr(self.session, method)
        r = _method(url, params=params, data=data)
        try:
            r.raise_for_status()
        except Exception as e:
            print e.message
            return None
        try:
            data = r.json()
            m = re.match(self.base_url + '/(.*)', url)
            path = m.group(1).replace('/', '-') + '.json'
            # TODO Enable write below
            self._write(path, data)
        except ValueError:
            pass
        return r


class SSH_Base(object):
    def __init__(self, port=22, username=None, password=None, pem=None):
        if pem is not None:
            self._pem = StringIO.StringIO(pem)
            self._pem = paramiko.RSAKey.from_private_key(self._pem)
        else:
            self._pem = None
        self._ssh = None
        self._cmd_list = []
        self.local_ip = None
        self.local_port = port
        self.username = username
        self.password = password

    def run_commands(self):
        response_list = []
        for cmd in self._cmd_list:
            response_list.append(self._run_cmd(cmd, sudo=True))
        with open(self.local_ip + '.ssh.json', 'w') as fh:
            json.dump(response_list, fh)
        return response_list

    def _configure_ssh(self):
        ssh = paramiko.SSHClient()
        ssh.load_system_host_keys()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            if self.password is not None:
                ssh.connect(self.local_ip,
                            port=self.local_port,
                            username=self.username,
                            password=self.password)
            elif self._pem is not None:
                ssh.connect(self.local_ip,
                            port=self.local_port,
                            username=self.username,
                            pkey=self._pem)
            print "Connected to host: %s" % self.local_ip
        except Exception as e:
            print "Failed to connect to host: %s" % self.local_ip
            print e.message
            ssh = None
        return ssh

    def _run_cmd(self, command, sudo=False):
        cmd = None
        if self._ssh is not None:
            if sudo:
                command = 'sudo ' + command
            # TODO print is just for some feedback
            print command
            cmd = {'command': command}
            sin, sout, serr = self._ssh.exec_command(command, get_pty=True)
            if sudo and self.password is not None:
                sin.write(self.password + '\n')
                sin.flush()
            cmd['response'] = sout.read()
        return cmd


class AviController(SSH_Base):
    def __init__(self, local_ip, port=22, username='admin', password=None, controllers=None):
        # TODO
        # Port: 5098 when running controller in a container
        super(AviController, self).__init__(port=port, username=username, password=password)
        self.local_ip = local_ip
        self.controllers = controllers
        self._cmd_list = ['hostname',
                          'ls -ail /opt/avi/log/*',
                          'ps -aux',
                          'top -b -o +%MEM | head -n 22',
                          '/opt/avi/scripts/taskqueue.py -s',
                          # TODO sort out the cc log name from the config
                          # 'grep "pending changes" /opt/avi/log/cc_agent_Default-Cloud.log',
                          'df -h']
        self._ssh = self._configure_ssh()
        self.command_list = self.run_commands()
        for p in self.ping_controllers():
            self.command_list.append(p)
        try:
            self._ssh.close()
        except Exception as e:
            pass

    def ping_controllers(self):
        ctrl_list = []
        for ip in self.controllers:
            if ip is not self.local_ip:
                ctrl_list.append(self._run_cmd('ping -c5 %s' % ip))
        with open(self.local_ip + '.ping.json', 'w') as fh:
            json.dump(ctrl_list, fh)
        return ctrl_list


class AviSE(SSH_Base):
    def __init__(self, local_ip, port=5097, username='admin', password=None, controllers=None):
        super(AviSE, self).__init__(port=port, username=username, password=password)
        self.local_ip = local_ip
        self.controllers = controllers
        self._cmd_list = ['hostname',
                          'docker info',
                          'iptables -nvL',
                          'iptables -nvL -t nat',
                          'ip route show table all',
                          'ifconfig',
                          'ip link',
                          'ip addr',
                          'sysctl -a',
                          'df -h',
                          'ls -ail /opt/avi/log',
                          'date',
                          'ntpq -p']
        self._ssh = self._configure_ssh()
        self.command_list = self.run_commands()
        for p in self.ping_controllers():
            self.command_list.append(p)
        try:
            self._ssh.close()
        except Exception as e:
            pass

    def ping_controllers(self):
        ctrl_list = []
        for ip in self.controllers:
            ctrl_list.append(self._run_cmd('ping -c5 %s' % ip))
        with open(self.local_ip + '.ping.json', 'w') as fh:
            json.dump(ctrl_list, fh)
        return ctrl_list


class K8sNode(SSH_Base):
    def __init__(self, local_ip, port=22, username=None, password=None, pem=None, controllers=None):
        super(K8sNode, self).__init__(port=port, username=username, password=password, pem=pem)
        self.local_ip = local_ip
        self.controllers = controllers
        self._cmd_list = ['hostname',
                          'docker info',
                          'iptables -nvL',
                          'iptables -nvL -t nat',
                          'ip route show table all',
                          'ifconfig',
                          'ip link',
                          'ip addr',
                          'sysctl -a',
                          'df -h',
                          'date',
                          'ntpq -p']
        self._ssh = self._configure_ssh()
        self.command_list = self.run_commands()
        for p in self.ping_controllers():
            self.command_list.append(p)
        try:
            self._ssh.close()
        except Exception as e:
            pass

    def ping_controllers(self):
        ctrl_list = []
        for ip in self.controllers:
            ctrl_list.append(self._run_cmd('ping -c5 %s' % ip))
        with open(self.local_ip + '.ping.json', 'w') as fh:
            json.dump(ctrl_list, fh)
        return ctrl_list


class K8s(object):
    def __init__(self, k8s_cloud=None):
        self._kauth = kubernetes.client.Configuration()
        self._kauth.host = 'https://' + k8s_cloud['oshiftk8s_configuration']['master_nodes'][0]
        self._kauth.verify_ssl = False
        self._kauth.api_key['authorization'] = k8s_cloud['oshiftk8s_configuration']['service_account_token']
        self._kauth.api_key_prefix['authorization'] = 'Bearer'

        self._oauth = kubernetes.client.Configuration()
        self._oauth.host = 'https://' + k8s_cloud['oshiftk8s_configuration']['master_nodes'][0]
        self._oauth.verify_ssl = False
        self._oauth.api_key['authorization'] = k8s_cloud['oshiftk8s_configuration']['service_account_token']
        self._oauth.api_key_prefix['authorization'] = 'Bearer'

        self.v1Api = kubernetes.client.CoreV1Api(kubernetes.client.ApiClient(self._kauth))
        self.nodes = self._k8s_api(self.v1Api, 'list_node')
        self.services = self._k8s_api(self.v1Api, 'list_service_for_all_namespaces')
        self.serviceaccounts = self._k8s_api(self.v1Api, 'list_serviceaccount')

        self.oapi = openshift.client.OapiApi(openshift.client.ApiClient(self._oauth))
        self.projects = self._k8s_api(self.oapi, 'list_project')

    def _k8s_api(self, api, cmd):
        try:
            response = getattr(api, cmd)()
            flat = kubernetes.client.ApiClient().sanitize_for_serialization(response)
            with open('k8s-' + cmd + '.json', 'w') as fh:
                json.dump(flat, fh)
            return response
        except Exception as e:
            print 'K8s exception with %s' % cmd
            print e.message


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--controller', type=str, default='127.0.0.1')
    parser.add_argument('--username', type=str, default='admin')
    parser.add_argument('--password', type=str, default=None)
    parser.add_argument('--output', type=str, default='')
    parser.add_argument('--api-version', type=str, default='17.2.10')
    parser.add_argument('--tenant', type=str, default='*')
    parser.add_argument('--infra_label', type=str, default='region=infra')
    parser.add_argument('--secure_channel_port', type=int, default=5097)
    args = parser.parse_args()

    avi = Avi(host=args.controller, username=args.username, password=args.password,
              out_dir=args.output, tenant=args.tenant, avi_api_version=args.api_version)
