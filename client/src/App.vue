<script setup lang="ts">
import { onLaunch } from '@dcloudio/uni-app'

onLaunch(() => {
  // 这里刻意什么都不做：票的判定在每个页面的 onShow 里（`stores/auth.ts` 的
  // bootstrap）。放在 onLaunch 里做跳转的话，H5 上链接直接带 ?code= 进来的那
  // 条路径会和它抢着 reLaunch，表现为「扫码进来先闪一下首页」。
})
</script>

<style>
page {
  background-color: #f5f6f8;
  color: #1f2329;
  font-size: 28rpx;
}
</style>
