import longhorn
import warnings
import contextlib
import requests
import traceback
import tempfile
import json
import logging
import os
import sys
import time
import yaml

from urllib3.exceptions import InsecureRequestWarning

old_merge_environment_settings = requests.Session.merge_environment_settings


@contextlib.contextmanager
def no_ssl_verification():
    opened_adapters = set()

    def merge_environment_settings(self, url, proxies, stream, verify, cert):
        # Verification happens only once per connection so we need to close
        # all the opened adapters once we're done. Otherwise, the effects of
        # verify=False persist beyond the end of this context manager.
        opened_adapters.add(self.get_adapter(url))

        settings = old_merge_environment_settings(self, url, proxies, stream, verify, cert)
        settings['verify'] = False

        return settings

    requests.Session.merge_environment_settings = merge_environment_settings

    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', InsecureRequestWarning)
            yield
    finally:
        requests.Session.merge_environment_settings = old_merge_environment_settings

        for adapter in opened_adapters:
            try:
                adapter.close()
            except:
                pass


class LonghornClient(longhorn.Client):


    VOLUME_STATE_ATTACHED = "attached"
    VOLUME_STATE_DETACHED = "detached"


    def __init__(self, url: str):
        super().__init__(url=url)
        self.logger = logging.getLogger(__name__)
        self.retry_inverval_in_seconds = 1
        self.retry_counts = 180
        self.wait_detached_volumes = {}


    def get_backup_volumes_by_pvc_name(self, pvc_name: str) -> list:
        backup_volumes = self.list_backupVolume()
        result = []
        for backup_volume in backup_volumes:
            try:
                backup_pvc_name = json.loads(backup_volume["labels"]["KubernetesStatus"])["pvcName"]
                if backup_pvc_name == pvc_name:
                    result.append(backup_volume)
            except Exception as e:
                self.logger.error('Error as get_backup_volumes_by_pvc_name', exc_info=e)
                # print(traceback.format_exc())

        return result


    def get_available_backup_volumes_pvc_names(self) -> list:
        backup_volumes = self.list_backupVolume()
        result = []
        for backup_volume in backup_volumes:
            try:
                result.append(json.loads(backup_volume["labels"]["KubernetesStatus"])["pvcName"])
            except Exception as e:
                self.logger.error('Error as get_backup_volumes_by_pvc_name', exc_info=e)
                # print(traceback.format_exc())

        return result


    def wait_for_volume_creation(self, volume_name: str) -> None:
        for _ in range(self.retry_counts):
            volumes = self.list_volume()
            for volume in volumes:
                if volume.name == volume_name:
                    return
            time.sleep(self.retry_inverval_in_seconds)
        raise FileNotFoundError(f"{volume_name} not found")


    def wait_for_volume_status(self, volume_name: str, value) -> dict:
        self.wait_for_volume_creation(volume_name)
        for _ in range(self.retry_counts):
            volume = self.by_id_volume(volume_name)
            self.logger.debug(f"Volume {volume_name} state: %s", str(volume["state"]))
            if isinstance(value, list):
                if any(volume["state"] == x for x in value):
                    return volume
            else:
                if volume["state"] == value:
                    return volume
            time.sleep(self.retry_inverval_in_seconds)
        raise TimeoutError(f"{volume_name} volume does not satisfy condition {value}")


    def wait_for_volume_detached_or_atached(self, volume_name: str) -> dict:
        self.logger.info(f"Wait for volume {volume_name} in detached/attached state")
        return self.wait_for_volume_status(volume_name, [self.VOLUME_STATE_ATTACHED, self.VOLUME_STATE_DETACHED])


    def wait_volume_kubernetes_status(self, volume_name: str, expect_ks: dict) -> None:
        for _ in range(self.retry_counts):
            expected = True
            volume = self.by_id_volume(volume_name)
            ks = volume.kubernetesStatus
            ks = json.loads(json.dumps(ks, default=lambda o: o.__dict__))

            for k, v in expect_ks.items():
                self.logger.debug(f"{volume_name} {k}: {ks[k]}")
                if k in ('lastPVCRefAt', 'lastPodRefAt'):
                    if (v != '' and ks[k] == '') or (v == '' and ks[k] != ''):
                        expected = False
                        break
                else:
                    if k == "pvStatus":
                        if isinstance(ks[k], list):
                            if not any(x == v for x in ks[k]):
                                expected = False
                                break
                    if ks[k] != v:
                        expected = False
                        break
            if expected:
                break

            time.sleep(self.retry_inverval_in_seconds)

        if not expected:
            raise TimeoutError(f"{volume_name} volume does not satisfy condition {expect_ks}")


    def create_pv_for_volume(self, volume, pv_name, fs_type="ext4") -> None:
        try:
            volume.pvCreate(pvName=pv_name, fsType=fs_type)
        except Exception as ex:
            self.logger.info(str(ex))

        ks = {
            'pvName': pv_name,
            'pvStatus': ['Available', 'Bound'],
            'lastPVCRefAt': '',
            'lastPodRefAt': '',
        }
        self.logger.info(f"Wait for PV {pv_name}")
        self.wait_volume_kubernetes_status(volume.name, ks)


    def create_pvc_for_volume(self, volume, pvc_namespace:str, pvc_name: str) -> None:
        try:
            volume.pvcCreate(namespace=pvc_namespace, pvcName=pvc_name)
        except Exception as ex:
            self.logger.info(str(ex))

        ks = {
            'pvStatus': 'Bound',
            'lastPVCRefAt': '',
        }
        self.logger.info(f"Wait for PVC {pvc_name}")
        self.wait_volume_kubernetes_status(volume.name, ks)


    def get_backup_by_volume_name(self, volume_name):
        bv = self.by_id_backupVolume(id=volume_name)
        if bv.lastBackupName:
            return bv.backupGet(name=bv.lastBackupName)
        else:
            return None


    def prepare_volume(self, volume_name: str, config: dict):
        restore = config["restore"] if "restore" in config else False
        backup = self.get_backup_by_volume_name(volume_name)

        if self.by_id_volume(id=volume_name):
            self.logger.info(f"Volume '{volume_name}' already exists, skip create volume process")
        elif backup and restore:
            self.logger.info(f"Use existing backup for volume: {volume_name}")
            self.create_volume(name=volume_name, size=config["size"], fromBackup=backup.url)
        else:
            self.logger.info(f"Create new empty volume: {volume_name}")
            self.create_volume(name=volume_name, size=config["size"])

        self.wait_detached_volumes[volume_name] = json.loads(backup.labels.KubernetesStatus) \
                if backup is not None else None


    def finalize_volume(self, volume_name: str, config: dict):
        if volume_name not in self.wait_detached_volumes:
            return

        createPV = config["createPV"] if "createPV" in config else True
        createPVC = config["createPVC"] if "createPVC" in config else False
        groups = config["groups"] if "groups" in config else []

        volume = self.wait_for_volume_detached_or_atached(volume_name)
        if groups:
            for groupName in groups:
                self.logger.info(f"Assign group {groupName} to volume {volume_name}")
                volume.recurringJobAdd(name=groupName, isGroup=True)

        if createPV:
            pvName = config["pvName"] if "pvName" in config else volume_name
            self.logger.info(f"Create PersistentVolume {pvName}")
            self.create_pv_for_volume(volume, pvName)
            self.logger.info(f"Label PV {pvName} with app={pvName}")
            os.system(f"kubectl label pv {pvName} app={pvName}")
            self.logger.info(f"Label PV {pvName} with app.kubernetes.io/name={pvName}")
            os.system(f"kubectl label pv {pvName} app.kubernetes.io/name={pvName}")
            if "claimRef" in config:
                pvcNamespace = config["namespace"]
                claimRef = config["claimRef"]
                self.logger.info(f"Add claimRef {claimRef} to PV {pvName}")
                patch_file = 'override.yaml'
                os.system(f"kubectl get pv {pvName} -n {pvcNamespace} -o yaml > {patch_file}")
                # os.system(f"cat {patch_file}")
                with open(patch_file, 'r') as fd:
                    patch = yaml.safe_load(fd)
                    if patch is None:
                        self.logger.error("yaml parser failed for content: %s", str(fd.readlines()))

                patch["spec"]["claimRef"] = {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "name": claimRef,
                    "namespace": pvcNamespace
                }

                with open(patch_file, 'w') as fd:
                    yaml.dump(patch, fd)

                # os.system(f"cat {patch_file}")
                os.system(f"kubectl replace -f {patch_file}")
                os.remove(patch_file)


        if createPVC:
            pvcName = config["pvcName"] if "pvcName" in config else volume_name
            pvcNamespace = config["namespace"]
            self.logger.info(f"Create PersistentVolumeClaim {pvcNamespace}/{pvcName}")
            self.create_pvc_for_volume(volume, pvcNamespace, pvcName)

        del self.wait_detached_volumes[volume_name]



class LonghornVolumeManager:

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        volumes_config_path = os.getenv('VOLUMES_CONFIG_PATH', '/config/volumes.yaml')
        self.logger.info("VOLUMES_CONFIG_PATH=%s", volumes_config_path)
        self._load_config(volumes_config_path)


    def _wait_start_delay(self) -> None:
        start_delay_in_seconds = int(str(os.getenv('START_DELAY_IN_SECONDS', 0)))
        if start_delay_in_seconds > 0:
            self.logger.info('Wait %d seconds', start_delay_in_seconds)
            time.sleep(start_delay_in_seconds)


    def _load_config(self, config_path: str) -> None:
        with open(config_path, 'r') as fd:
            try:
                self.config = yaml.safe_load(fd)
            except yaml.YAMLError as e:
                self.logger.error('Parse volume config %s FAILED', config_path)
                raise e

        self.logger.debug("volume config: %s", str(self.config))

        for k in ["apiVersion", "kind", "spec"]:
            if k not in self.config:
                raise KeyError(f"'{k}' is not defined in volumes config")

        if self.config["apiVersion"] != "longhorn-volume-manager/v1":
            self.logger.error("Invalid apiVersion defined in volume config")
            raise ValueError(f"apiVersion: {self.config['apiVersion']}' not supported")

        if self.config["kind"] != "LonghornVolumeSpec":
            self.logger.error("Invalid kind defined in volume config")
            raise ValueError(f"'kind: {self.config['kind']}' not supported")

        for k in ["volumes"]:
            if k not in self.config["spec"]:
                raise KeyError(f"'{k}' is not defined in volumes config")


    def _setup_client_connection(self)-> None:
        longhorn_url = os.getenv('LONGHORN_URL', 'http://longhorn-frontend.longhorn-system/v1')
        self.logger.info("LONGHORN_URL=%s", longhorn_url)
        self.client = LonghornClient(url=longhorn_url)


    def _print_available_backup_volumes(self) -> None:
        available_backup_volumes =  list(map(lambda x: json.loads(x["labels"]["KubernetesStatus"])["pvName"], self.client.list_backupVolume()))
        self.logger.info('Available backup pv names: %s', str(available_backup_volumes))


    def _process_create_volumes(self) -> None:
        self._wait_start_delay()
        self._setup_client_connection()
        self._print_available_backup_volumes()
        for volume_id in self.config["spec"]["volumes"]:
            self.client.prepare_volume(volume_id, self.config["spec"]["volumes"][volume_id])
        for volume_id in self.config["spec"]["volumes"]:
            self.client.finalize_volume(volume_id, self.config["spec"]["volumes"][volume_id])
        self.logger.info("Volume setup completed")
        time.sleep(1)


    def create_volumes(self):
        disable_ssl_verification = bool(os.getenv('DISABLE_SSL_VERIFICATION', False))
        self.logger.info(f"DISABLE_SSL_VERIFICATION={disable_ssl_verification}")
        if disable_ssl_verification:
            with no_ssl_verification():
                self._process_create_volumes()
        else:
            self._process_create_volumes()


def setup_logging():
    logging.basicConfig(
        level=os.getenv('LOG_LEVEL', "INFO"),
        format='[%(asctime)s] {%(filename)s:%(lineno)d} %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(stream=sys.stdout)
        ]
    )


if __name__ == "__main__":
    setup_logging()
    volume_manager = LonghornVolumeManager()
    volume_manager.create_volumes()
