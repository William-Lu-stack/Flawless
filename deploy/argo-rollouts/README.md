# Argo Rollouts（国内镜像）

Flawless 的真实灰度发布依赖 Argo Rollouts CRD 与控制器。版本固定为
`v1.9.1`，控制器镜像使用 DaoCloud 的 Quay 代理，并锁定镜像摘要：

```text
quay.m.daocloud.io/argoproj/argo-rollouts:v1.9.1@sha256:15c0d41f2c69a382d4399bcb28ed4f03ee9f58b56cfc9e6cd55bcbf0f311c06d
```

联网环境直接安装：

```bash
chmod +x deploy/argo-rollouts/install-domestic.sh
deploy/argo-rollouts/install-domestic.sh
```

控制节点不能访问 GitHub、但可以拉国内镜像时，先在联网机器下载官方
`install.yaml`，再把文件复制到控制节点：

```bash
curl -fL \
  https://github.com/argoproj/argo-rollouts/releases/download/v1.9.1/install.yaml \
  -o argo-rollouts-v1.9.1-install.yaml

ARGO_ROLLOUTS_INSTALL_MANIFEST=./argo-rollouts-v1.9.1-install.yaml \
  deploy/argo-rollouts/install-domestic.sh
```

完全离线时，在可联网 Linux 机器拉取并导出镜像：

```bash
docker pull quay.m.daocloud.io/argoproj/argo-rollouts:v1.9.1
docker save \
  quay.m.daocloud.io/argoproj/argo-rollouts:v1.9.1 \
  -o argo-rollouts-v1.9.1.tar
```

把 tar 导入内网镜像仓库并设置脚本使用的镜像：

```bash
docker load -i argo-rollouts-v1.9.1.tar
docker tag \
  quay.m.daocloud.io/argoproj/argo-rollouts:v1.9.1 \
  registry.example.com/platform/argo-rollouts:v1.9.1
docker push registry.example.com/platform/argo-rollouts:v1.9.1

ARGO_ROLLOUTS_INSTALL_MANIFEST=./argo-rollouts-v1.9.1-install.yaml \
ARGO_ROLLOUTS_IMAGE=registry.example.com/platform/argo-rollouts:v1.9.1 \
  deploy/argo-rollouts/install-domestic.sh
```

安装后还需要把 `manifests/rbac.yaml` 中的受控权限绑定到 Flawless
ServiceAccount，并确保业务 namespace 位于 `ALLOWED_NAMESPACES`。
`GRAY_RELEASE_ANALYSIS_URL` 必须是目标集群内 AnalysisRun 能访问的
Flawless API Service 地址。
