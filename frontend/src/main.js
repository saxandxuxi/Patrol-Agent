import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import dayjs from 'dayjs'
import isoWeek from 'dayjs/plugin/isoWeek'
import quarterOfYear from 'dayjs/plugin/quarterOfYear'
import 'element-plus/dist/index.css'
import App from './App.vue'
import './styles.css'

dayjs.extend(isoWeek)
dayjs.extend(quarterOfYear)

createApp(App).use(ElementPlus).mount('#app')
