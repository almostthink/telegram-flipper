import { Page, Planned } from '../components/Page'

export default function AutoTrade() {
  return (
    <Page
      title="Автомат"
      subtitle="Автоматическая торговля под жёсткими лимитами"
    >
      <Planned
        stage={7}
        items={[
          'Тумблер включения — недоступен, пока не пройден paper-режим',
          'Дневной бюджет, максимум на сделку, лимит открытых позиций',
          'Whitelist коллекций: пустой список означает запрет торговли',
          'Circuit breaker по числу ошибок и по дневному убытку',
          'Kill-switch: мгновенная остановка и снятие всех заявок',
        ]}
      />
    </Page>
  )
}
