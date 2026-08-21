<script setup lang="ts">
import { onShow } from '@dcloudio/uni-app'
import { ref } from 'vue'

import { useAuthStore } from '../../stores/auth'

const auth = useAuthStore()

/**
 * 退出用两步而不是弹窗确认。
 *
 * `uni.showModal` 在小程序上是原生模态，在 H5 上是自绘的 —— 两端的行为和外观都
 * 不一样，而这个操作只有一句话要说。按钮自己变成「再点一次确认」，两端一致。
 */
const armed = ref(false)

onShow(() => {
  armed.value = false
  void auth.bootstrap()
})

function signOut(): void {
  if (!armed.value) {
    armed.value = true
    return
  }
  auth.signOut()
  uni.reLaunch({ url: '/pages/redeem/redeem' })
}
</script>

<template>
  <view class="wrap">
    <view class="header">
      <view class="title">{{ auth.clientName || '我' }}</view>
      <view class="subtitle">这份数据怎么读</view>
    </view>

    <view class="card">
      <view class="item-title">日期是广告账户的时区，不是你的时区</view>
      <view class="item-body">
        每个广告账户有自己的时区，一个客户名下的账户也可能各不相同。看板上的日期一律
        按<text class="em">账户自己</text>的时区切分自然日 —— 所以拿它和你手机上的日历对，可能差一天。
        每个账户的时区就写在它的卡片上。
      </view>
    </view>

    <view class="card">
      <view class="item-title">和你自己后台的数字对不上是正常的</view>
      <view class="item-body">
        归因窗口、浏览归因、跨设备、两个平台同时给同一单邀功 —— 广告平台报表相加大于
        实际订单数是这个行业的常态。正确的做法是两个数字都看着，并且知道差异从哪来，
        而不是想办法让它们一致。
      </view>
    </view>

    <view class="card">
      <view class="item-title">余额还能撑几天，是这么算的</view>
      <view class="item-body">
        可用余额 ÷ 近期日均消耗。日均<text class="em">不含今天</text>（今天还没跑完），分母是那段时间里
        真正有数据的天数。卡片上那行小字会写清用的是哪几天 —— 如果有数据的天数明显偏
        少，这个日均就要打个问号。
      </view>
    </view>

    <view class="card">
      <view class="item-title">数据不是实时的</view>
      <view class="item-body">
        投放数据由你的投放负责人从平台后台导入，导一次更新一次。看板上没有的那天不是
        「花了 0」，是「还没导入」—— 明细页里会分开标注。
      </view>
    </view>

    <button class="signout" :class="{ armed }" @tap="signOut">
      {{ armed ? '再点一次，确认退出' : '退出登录' }}
    </button>

    <view class="footnote">
      退出只会清掉这台设备上的登录状态。要重新进来，找投放负责人再要一个邀请码。
    </view>
  </view>
</template>

<style scoped>
.wrap {
  padding: 32rpx 24rpx 64rpx;
}
.header {
  padding: 8rpx 8rpx 24rpx;
}
.title {
  font-size: 40rpx;
  font-weight: 600;
}
.subtitle {
  color: #6b7280;
  margin-top: 8rpx;
}
.card {
  background: #ffffff;
  border-radius: 16rpx;
  padding: 28rpx;
  margin-bottom: 20rpx;
}
.item-title {
  font-size: 28rpx;
  font-weight: 600;
}
.item-body {
  color: #4b5563;
  line-height: 1.75;
  margin-top: 12rpx;
}
/* 模板里不能写 markdown 的 **，那会原样显示成两个星号（有门禁盯着）。 */
.em {
  color: #1f2329;
  font-weight: 600;
}
.signout {
  background: #ffffff;
  color: #6b7280;
  border-radius: 12rpx;
  margin-top: 32rpx;
}
.signout.armed {
  color: #d1242f;
}
.footnote {
  color: #9ca3af;
  font-size: 22rpx;
  line-height: 1.6;
  padding: 24rpx 8rpx;
}
</style>
