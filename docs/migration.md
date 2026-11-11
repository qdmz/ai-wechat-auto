# 手机聊天记录迁移到 wxtest

微信「迁移聊天记录到电脑」要求手机和电脑在**同一个局域网**：电脑点「导入」后，容器里的微信监听 `0.0.0.0:8015`，把自己的网卡 IP 告诉手机，由**手机主动连**过来。云端容器 IP 是 `172.17.x.x`，手机直接报「无法连接，需同一网络」。

试过不行的：SOCKS5 代理（方向反了，是手机连电脑）；Tailscale 子网路由让手机直连容器 IP（微信手机端判定不同网，一次都没连进来）。

## 做法：给容器一个「家里的 IP」

在家里局域网挑一个没人用的地址（下文 `192.168.3.250`），**容器和家里一台常开的 Linux 设备（旁路由/NAS/小主机）都配上它**：

- 容器里微信看到自己有 `192.168.3.250`，就把这个地址告诉手机；
- 手机在家里 Wi-Fi 连 `192.168.3.250:8015`，被那台设备接住，经 Tailscale 转到云机，再转进容器。

```
手机(家里 Wi-Fi) → 192.168.3.250:8015 → 家里设备 DNAT → Tailscale → 云机 DNAT → 容器:8015
```

前提：家里设备和云机都装了 Tailscale 且互通；容器带 `--cap-add NET_ADMIN`。

下文变量自己换：`FAKE`=借用的家里 IP，`LAN`=家里设备网卡，`TS`=云机的 Tailscale IP，`CIP`=容器 IP（`docker inspect -f '{{.NetworkSettings.IPAddress}}' wxtest`）。

### 1. 家里设备（以 OpenWrt 为例）

```sh
FAKE=192.168.3.250; LAN=eth0; TS=100.x.x.x
ip addr add $FAKE/32 dev $LAN
sysctl -w net.ipv4.ip_forward=1
nft add table ip wxmig
nft add chain ip wxmig pre '{ type nat hook prerouting priority -100; }'
nft add chain ip wxmig post '{ type nat hook postrouting priority 100; }'
nft add rule ip wxmig pre ip daddr $FAKE tcp dport 8015 dnat to $TS:8015
nft add rule ip wxmig post ip daddr $TS tcp dport 8015 masquerade
nft insert rule inet fw4 forward tcp dport 8015 accept   # OpenWrt 防火墙放行；别的系统按需
```

### 2. 云机（容器所在宿主）

```sh
CIP=172.17.0.2
sudo iptables -t nat -A PREROUTING -i tailscale0 -p tcp --dport 8015 -j DNAT --to-destination $CIP:8015
sudo iptables -I DOCKER-USER -i tailscale0 -p tcp --dport 8015 -j ACCEPT
```

### 3. 容器

```sh
sudo nsenter -t $(docker inspect -f '{{.State.Pid}}' wxtest) -n ip addr add 192.168.3.250/32 dev eth0
```

自检：家里设备上 `nc -zv $TS 8015` 能通（要先在微信里点了「导入」才有人监听）。

### 4. 导入

1. 在控制面板（`/panel`）打开远程桌面（选「要操作」），微信左下 ≡ →「聊天记录管理」→「导入聊天记录」；
2. 手机连家里 Wi-Fi（iOS 要给微信开「本地网络」权限），按提示选要迁移的聊天；
3. 电脑端显示「导入已完成」后关掉迁移窗口。

记录写进 `message_1.db`…`message_N.db`（号越大越早），`/messages`、`/search`、图片、文件接口自动都能读到，**不用改代码**；这些消息的 `id` 是负数。

## 清理（规则都是临时的，重启也会丢）

```sh
# 家里设备
ip addr del 192.168.3.250/32 dev eth0; nft delete table ip wxmig
nft -a list chain inet fw4 forward | grep 8015   # 记下 handle，再 nft delete rule inet fw4 forward handle <N>
# 云机
sudo iptables -t nat -D PREROUTING -i tailscale0 -p tcp --dport 8015 -j DNAT --to-destination 172.17.0.2:8015
sudo iptables -D DOCKER-USER -i tailscale0 -p tcp --dport 8015 -j ACCEPT
# 容器
sudo nsenter -t $(docker inspect -f '{{.State.Pid}}' wxtest) -n ip addr del 192.168.3.250/32 dev eth0
```

## 注意

- 全量迁移可能十几 GB：数据目录放大盘（例如 `-v /data/wxtest:/data`），导入前看好剩余空间。
- 重建容器要保持同一个 `--hostname`、同一个数据目录，否则要重新扫码；容器 IP 变了，第 2、3 步要用新 IP 重做。
- 借用的 IP 必须是家里没人用的，用完删掉，免得冲突。
