import { Page, Planned } from '../components/Page'

export default function Settings() {
  return (
    <Page title="Настройки" subtitle="Доступы, комиссии и пороги отбора">
      <Planned
        stage={1}
        items={[
          'Подключение Telegram: api_id и api_hash хранятся локально в Windows DPAPI',
          'Авто-обновление tma-токена площадок через userbot-сессию',
          'HAR-импорт для восстановления эндпоинтов MRKT',
          'Комиссии площадок и оценка газа TON',
          'Пороги отбора: минимальный ROI, ликвидность, максимальный TTS',
          'Переключатель paper/live режима',
        ]}
      />
    </Page>
  )
}
