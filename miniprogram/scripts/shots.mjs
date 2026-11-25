#!/usr/bin/env node
/**
 * 一条命令出全套截图（尽量不重启开发者工具）：
 *   1) 先复用已开着的自动化会话（工具开了「自动热重载」，保存即编译）
 *   2) 会话不可用 / 截图超时，才重启一次 CLI 自动化并重连
 *   3) 逐页 reLaunch + 截图，输出到 %USERPROFILE%\.dsh\shots
 *
 * 用法：node scripts/shots.mjs [page ...]
 *   node scripts/shots.mjs                 # 默认四 Tab + 待处理 + 盘点
 *   node scripts/shots.mjs goods pending   # 只截指定页
 */

import { spawn } from 'node:child_process'
import { connect as netConnect } from 'node:net'
import { mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join, resolve } from 'node:path'
import automator from 'miniprogram-automator'

const CLI = process.env.WECHAT_CLI || 'D:\\wechatSDK\\cli.bat'
const PROJECT = resolve(process.cwd(), '..')
const OUT_DIR = join(homedir(), '.dsh', 'shots')
const AUTO_PORT = Number(process.env.WECHAT_AUTO_PORT || 9420)
const WS = `ws://127.0.0.1:${AUTO_PORT}`

const PAGES = {
  index: '/pages/index/index',
  goods: '/pages/goods/index',
  debts: '/pages/debts/index',
  mine: '/pages/mine/index',
  pending: '/pages/pending/index',
  count: '/pages/count/index',
  card: '/pages/card/index',
  manual: '/pages/manual/index',
  query: '/pages/query/index',
  pay: '/pages/pay/index',
  admin: '/pages/admin/index',
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function portOpen(port) {
  return new Promise((done) => {
    const socket = netConnect({ host: '127.0.0.1', port }, () => {
      socket.destroy()
      done(true)
    })
    socket.on('error', () => done(false))
    socket.setTimeout(800, () => {
      socket.destroy()
      done(false)
    })
  })
}

function startAuto() {
  spawn('cmd.exe', ['/c', CLI, 'auto', '--project', PROJECT, '--auto-port', String(AUTO_PORT)], {
    detached: true,
    stdio: 'ignore',
    windowsHide: true,
  }).unref()
}

/** 连上并探活（reLaunch 今天页），失败返回 null。 */
async function connectOnce() {
  if (!(await portOpen(AUTO_PORT))) {
    return null
  }
  try {
    const mp = await automator.connect({ wsEndpoint: WS })
    await mp.reLaunch('/pages/index/index')
    return mp
  } catch (error) {
    console.log(`connect failed: ${error.message}`)
    return null
  }
}

/** 重启自动化并重连（最多等 90 秒）。 */
async function restartAndConnect() {
  startAuto()
  for (let i = 0; i < 60 && !(await portOpen(AUTO_PORT)); i += 1) {
    await sleep(1000)
  }
  if (!(await portOpen(AUTO_PORT))) {
    throw new Error(`自动化端口 ${AUTO_PORT} 没起来：确认开发者工具「服务端口」已开`)
  }
  await sleep(6000) // 给首次编译留时间
  for (let i = 0; i < 8; i += 1) {
    const mp = await connectOnce()
    if (mp !== null) {
      console.log('connected after restart')
      return mp
    }
    await sleep(4000)
  }
  throw new Error('automator 连接失败')
}

async function main() {
  const wanted = process.argv.slice(2)
  const names = wanted.length > 0 ? wanted : ['index', 'goods', 'debts', 'mine', 'pending', 'count']
  mkdirSync(OUT_DIR, { recursive: true })

  let mp = await connectOnce()
  if (mp !== null) {
    console.log('reused existing automation session')
  } else {
    mp = await restartAndConnect()
  }

  let restarted = false
  for (const name of names) {
    const route = PAGES[name]
    if (route === undefined) {
      console.log(`skip unknown page: ${name}`)
      continue
    }
    const shoot = async () => {
      await mp.reLaunch(route)
      await sleep(1800)
      const file = join(OUT_DIR, `${name}.png`)
      await mp.screenshot({ path: file })
      console.log(`shot ${name} -> ${file}`)
    }
    try {
      await shoot()
    } catch (error) {
      if (restarted) {
        throw error
      }
      restarted = true
      console.log(`page ${name} failed (${error.message}), restarting automation once…`)
      mp = await restartAndConnect()
      await shoot()
    }
  }

  process.exit(0)
}

main().catch((error) => {
  console.error(error.message)
  process.exit(1)
})
