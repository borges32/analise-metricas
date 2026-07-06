import { AppPlugin } from '@grafana/data';

import { App } from './components/App';
import { ConfigPage } from './components/ConfigPage';
import { AppJsonData } from './types';

export const plugin = new AppPlugin<AppJsonData>()
  .setRootPage(App as any)
  .addConfigPage({
    title: 'Configuração',
    icon: 'cog',
    body: ConfigPage as any,
    id: 'config',
  });
