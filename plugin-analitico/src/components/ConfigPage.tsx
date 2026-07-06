import React, { useState } from 'react';
import { AppPluginMeta, PluginConfigPageProps, PluginMeta } from '@grafana/data';
import { DataSourcePicker, getBackendSrv, locationService } from '@grafana/runtime';
import { Alert, Button, Field, useStyles2 } from '@grafana/ui';
import { css } from '@emotion/css';

import { AppJsonData } from '../types';
import { PLUGIN_ID } from '../constants';

type Props = PluginConfigPageProps<AppPluginMeta<AppJsonData>>;

export function ConfigPage({ plugin }: Props) {
  const styles = useStyles2(getStyles);
  const [uid, setUid] = useState<string | undefined>(plugin.meta.jsonData?.datasourceUid);
  const [salvando, setSalvando] = useState(false);
  const [ok, setOk] = useState(false);

  const salvar = async () => {
    setSalvando(true);
    setOk(false);
    try {
      await getBackendSrv().post(`/api/plugins/${PLUGIN_ID}/settings`, {
        enabled: true,
        pinned: true,
        jsonData: { datasourceUid: uid },
      });
      setOk(true);
      // recarrega para o Grafana aplicar o jsonData no app
      locationService.reload();
    } finally {
      setSalvando(false);
    }
  };

  return (
    <div className={styles.wrap}>
      <p className={styles.help}>
        Selecione o datasource <b>PostgreSQL</b> que aponta para o banco de análise (schema{' '}
        <code>metricas</code>). O usuário do datasource precisa apenas de{' '}
        <code>SELECT</code> + <code>EXECUTE</code> no schema.
      </p>

      <Field label="Datasource PostgreSQL">
        <DataSourcePicker
          type="postgres"
          current={uid}
          noDefault
          onChange={(ds) => setUid(ds.uid)}
        />
      </Field>

      <Button onClick={salvar} disabled={!uid || salvando} icon={salvando ? 'spinner' : 'save'}>
        Salvar
      </Button>

      {ok && (
        <Alert title="Configuração salva" severity="success" className={styles.alert}>
          Datasource vinculado ao app.
        </Alert>
      )}
    </div>
  );
}

const getStyles = () => ({
  wrap: css({ maxWidth: 640 }),
  help: css({ marginBottom: 16 }),
  alert: css({ marginTop: 16 }),
});
