#!/usr/bin/env bash
# TLS 公共 helper —— install_autostart.sh / scripts/gen_tls_ca.sh /
# scripts/tls_cert_check.sh 三处共用。只定义函数，source 时无副作用。
#
# 单独一个文件的原因：这三处都要回答同样的两个问题 ——
# 「板子对外的 IP 到底是哪个」「证书的 SAN 里有没有这个 IP」。
# 各写各的必然漂移（IP 探测已经漂移过一次）。

# 板子对外 IP 的候选列表，primary 在第一行。
#
# 不用 `hostname -I | awk '{print $1}'`：它按任意顺序列出本机所有 IP，
# 多网卡时第一个未必是对外那个 ⇒ 证书 SAN 覆盖不到用户实际访问的地址。
# 优先级：默认路由的出口源地址 → 默认路由的网卡 → 全局地址 → hostname -I。
wdn_tls_detect_ips() {
    local primary="" dev=""
    if command -v ip >/dev/null 2>&1; then
        primary="$(ip route get 1.1.1.1 2>/dev/null \
            | sed -n 's/.*[[:space:]]src \([0-9a-fA-F:.]\{3,\}\).*/\1/p' | head -1)"
        if [ -z "$primary" ]; then
            dev="$(ip route show default 2>/dev/null \
                | sed -n 's/.*[[:space:]]dev \([^ ]*\).*/\1/p' | head -1)"
            [ -n "$dev" ] && primary="$(ip -4 addr show dev "$dev" 2>/dev/null \
                | sed -n 's/.*[[:space:]]inet \([0-9.]\{1,\}\).*/\1/p' | head -1)"
        fi
    fi
    {
        [ -n "$primary" ] && printf '%s\n' "$primary"
        if command -v ip >/dev/null 2>&1; then
            ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1
            ip -6 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1
        fi
        hostname -I 2>/dev/null
    } | awk 'NF && !seen[$0]++'
}

# primary（候选列表的第一行）。拿不到就返回空串，由调用方报错。
wdn_tls_detect_ip() {
    wdn_tls_detect_ips | head -1
}

# 证书的 SAN 里是否**精确**包含某 IP。
#
# 不能用 `grep "IP Address:$ip"`：那是子串匹配，IP 为 172.16.9.1 时会被
# 证书里的 172.16.9.117 命中 ⇒ 误判「已覆盖」⇒ 不重签 ⇒ 浏览器照样报
# 「证书名称与域名不符」。
wdn_tls_san_has_ip() {
    local cert="$1" ip="$2"
    [ -r "$cert" ] || return 1
    command -v openssl >/dev/null 2>&1 || return 1
    openssl x509 -in "$cert" -noout -ext subjectAltName 2>/dev/null \
        | tr ',' '\n' \
        | sed 's/^[[:space:]]*//; s/.*:[[:space:]]*//' \
        | grep -Fxq "$ip"
}

# 看起来像个合法 IP 吗？—— 参数写错时**当场报错**，不要留下半成品目录。
wdn_tls_is_ip() {
    local ip="$1" octet
    if printf '%s' "$ip" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
        for octet in $(printf '%s' "$ip" | tr '.' ' '); do
            [ "$octet" -gt 255 ] 2>/dev/null && return 1
        done
        return 0
    fi
    # IPv6：只做粗筛，合法性交给 openssl 判定
    printf '%s' "$ip" | grep -Eq '^[0-9a-fA-F:.]+$' && printf '%s' "$ip" | grep -q ':'
}
