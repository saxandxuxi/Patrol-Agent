# 小白级教程：SSH 登录服务器后，逐个访问数据库

> 适用：已通过 SSH 登录 10.168.210.78（openEuler 24）之后的操作
> 前提：你已经在服务器里，提示符长这样：`[root@xxx ~]#`

---

## 0. 先说三个通用要点

1. **所有命令都在服务器上执行**（不是你本机的 PowerShell）；
2. **密码统一不显示**：命令里的 `-p` 回车后手动输密码，屏幕没反应是正常的；
3. **先确认服务在跑**：任何服务访问前，先跑 `ss -tlnp | grep 端口`，有输出 = 在跑，没输出 = 服务没启动或端口不对。

---

## ① MySQL（端口 3307）

**连接**：
```bash
mysql -h127.0.0.1 -P3307 -uroot -p
```
（回车后输入密码）

**进去后的常用操作**：
```sql
show databases;                        -- 看所有数据库（第一步）
use 数据库名;                           -- 进入某个库
show tables;                           -- 看这个库的所有表
desc 表名;                             -- 看表结构（字段名、类型）
select * from 表名 limit 10;           -- 看前10行数据
show variables like '%version%';       -- 看 MySQL 版本
```

**退出**：`exit;` 回车

---

## ② PostgreSQL（端口 5436）

**连接**：
```bash
psql -h127.0.0.1 -p5436 -U postgres
```
（回车后输入密码）

**进去后的常用操作**：
```sql
\l                                   -- 看所有数据库
\c 数据库名                           -- 进入某个库
\dt                                  -- 看所有表
\d 表名                               -- 看表结构
select * from 表名 limit 10;         -- 看前10行数据
select version();                    -- 看 PG 版本
```

**退出**：`\q` 回车

---

## ③ Redis（端口 6376）

**连接**：
```bash
redis-cli -h127.0.0.1 -p6376 -a 密码
```

**进去后的常用操作**：
```
ping          -- 返回 PONG 说明连接正常
keys *        -- 看所有键（如果数据多会慢，慎用）
type 键名      -- 看这个键的类型（string/list/hash...）
get 键名       -- 取 string 类型的值
llen 键名      -- 看 list 长度
hgetall 键名   -- 看 hash 所有字段
```

**退出**：`quit`

---

## ④ EMQX 消息服务器（端口 6883、23083）

说明：这是 MQTT 消息中间件，6883/23083 是 MQTT 协议端口（不是网页）。命令行验证方法：

```bash
# 1. 看服务状态
/usr/bin/emqx ctl status

# 2. 看进程
ps -ef | grep emqx

# 3. 看有没有消息主题（如果装了 emqx_ctl 工具）
/usr/bin/emqx ctl topics list
```

**网页控制台**（一般 18083 端口，如果你从外面访问用的不是这个，问运维）：
浏览器打开 `http://10.168.210.78:18083`，默认账号 `admin / public`。

**智能体对接时**：用的是 MQTT 连接（6883），需要设备连接账号密码（不是网页密码），这个问业务方要。

---

## ⑤ RabbitMQ（端口 15672 网页、5672 通信）

**命令行验证**：
```bash
rabbitmqctl list_users      # 看所有用户
rabbitmqctl list_queues     # 看消息队列
rabbitmqctl status          # 看运行状态
```

**网页管理界面**：
浏览器打开 `http://10.168.210.78:15672`，用 list_users 查到的账号登录。

---

## ⑥ mediamtx 视频流（端口 18888、18322）

说明：流媒体服务器，一般无密码，协议端口不是 HTTP，命令行这样验证：

```bash
# 1. 确认在跑
ps -ef | grep mediamtx

# 2. 看配置（确认端口和有没有开 API）
cat /etc/mediamtx/mediamtx.yml    # 或 find / -name "mediamtx.yml" 2>/dev/null
```

**看视频流**：如果配置里有 RTSP 路径，用 VLC 打开 `rtsp://10.168.210.78:18888/路径` 即可预览。

---

## ⑦ Nacos（端口 6648、6748）

说明：服务注册/配置中心，用 HTTP 接口访问（不需要装客户端）。

```bash
# 1. 健康检查
curl http://127.0.0.1:6648/nacos/v1/console/health/readiness

# 2. 看所有服务（可能带命名空间）
curl "http://127.0.0.1:6648/nacos/v1/ns/service/list?pageNo=1&pageSize=100"

# 3. 看配置列表
curl "http://127.0.0.1:6648/nacos/v1/cs/configs?search=accurate&dataId=&group=&pageNo=1&pageSize=100"
```

**网页控制台**：浏览器打开 `http://10.168.210.78:6648/nacos`，默认账号 `nacos / nacos`。

---

## ⑧ Elasticsearch（端口 19200、9200）

说明：全部用 curl（HTTP），不用装客户端。

```bash
# 1. 健康状态
curl http://127.0.0.1:19200/_cat/health?v

# 2. 看所有索引（相当于"数据库列表"）★核心
curl http://127.0.0.1:19200/_cat/indices?v

# 3. 看某个索引的字段结构
curl http://127.0.0.1:19200/索引名/_mapping?pretty

# 4. 看某个索引的数据（前5条）
curl "http://127.0.0.1:19200/索引名/_search?pretty&size=5"

# 如果 ES 开了密码认证，加 -u：
curl -u elastic:密码 http://127.0.0.1:19200/_cat/indices?v
```

**索引名**：`_cat/indices?v` 输出的第一列就是索引名。

---

## ⑨ elasticsearch-head（端口 19100）

说明：ES 的网页可视化工具，**不用命令行、不用密码**。
浏览器打开 `http://10.168.210.78:19100`，页面里填 ES 地址（`http://10.168.210.78:19200`）点连接即可看索引和数据。

---

## ⑩ SeaweedFS 对象存储（端口 8899、23646）

说明：分布式对象存储，HTTP 接口，一般无密码。

```bash
# 1. master 状态
curl http://127.0.0.1:8899/cluster/status

# 2. 看 volume 状态
curl http://127.0.0.1:8899/vol/status

# 3. filer 文件服务（23646 可能是 filer 的 HTTP 端口）
curl http://127.0.0.1:23646/          # 有响应就是 filer 在跑
```

---

## 访问命令速查总表（收藏用）

| 服务 | 端口 | 连接命令 | 看数据 |
|---|---|---|---|
| MySQL | 3307 | `mysql -h127.0.0.1 -P3307 -uroot -p` | `show databases;` → `show tables;` |
| PostgreSQL | 5436 | `psql -h127.0.0.1 -p5436 -U postgres` | `\l` → `\dt` |
| Redis | 6376 | `redis-cli -h127.0.0.1 -p6376 -a 密码` | `keys *` |
| EMQX | 6883/23083 | `/usr/bin/emqx ctl status` | 网页 18083 |
| RabbitMQ | 15672/5672 | `rabbitmqctl list_users` | 网页 15672 |
| mediamtx | 18888/18322 | `ps -ef \| grep mediamtx` | VLC 拉流 |
| Nacos | 6648/6748 | `curl http://127.0.0.1:6648/nacos/...` | 网页 6648/nacos |
| Elasticsearch | 19200/9200 | `curl http://127.0.0.1:19200/` | `_cat/indices?v` |
| ES-head | 19100 | 网页直接访问 | 网页连接 ES |
| SeaweedFS | 8899/23646 | `curl http://127.0.0.1:8899/cluster/status` | 同上 |

---

## 特别提醒

1. **先 SSH 到服务器**：所有命令的前提是先登录：`ssh root@10.168.210.78`；
2. **找不到密码**：回顾上一篇文档（配置文件找密码法：`grep -i "password" /etc/xxx.conf`）；
3. **端口不对**：如果 `ss -tlnp | grep 3307` 没输出，说明该端口服务没起或监听的是别的端口，别硬连；
4. **MySQL 中文乱码**：连接时加 `--default-character-set=utf8mb4`；
5. **千万别在生产库上跑删除/更新**：`drop`、`delete`、`update` 开头的命令，先问清楚再执行。
