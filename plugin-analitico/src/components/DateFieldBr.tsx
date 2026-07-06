import React, { useState } from 'react';
import { GrafanaTheme2 } from '@grafana/data';
import { DatePicker, Icon, Input, useStyles2 } from '@grafana/ui';
import { css } from '@emotion/css';

import { dateParaYmd, formatarBr, ymdParaDate } from '../dates';

interface Props {
  value: string; // YYYY-MM-DD
  onChange: (ymd: string) => void;
  width?: number;
}

/**
 * Campo de data que SEMPRE exibe dd/MM/yyyy (independente do locale do Grafana),
 * com calendário do @grafana/ui. O DatePickerWithInput padrão não permite fixar
 * o formato (usa dateTime.format('L'), que é dependente de locale).
 */
export function DateFieldBr({ value, onChange, width = 16 }: Props) {
  const [open, setOpen] = useState(false);
  const styles = useStyles2(getStyles);

  return (
    <div className={styles.wrap}>
      <Input
        width={width}
        type="text"
        readOnly
        value={formatarBr(value)}
        placeholder="dd/mm/aaaa"
        onClick={() => setOpen((o) => !o)}
        suffix={<Icon name="calendar-alt" />}
      />
      {open && (
        <div className={styles.popover}>
          <DatePicker
            isOpen={open}
            value={ymdParaDate(value)}
            onChange={(d) => {
              onChange(dateParaYmd(d as Date));
              setOpen(false);
            }}
            onClose={() => setOpen(false)}
          />
        </div>
      )}
    </div>
  );
}

const getStyles = (theme: GrafanaTheme2) => ({
  wrap: css({ position: 'relative' }),
  popover: css({ position: 'absolute', top: '100%', left: 0, zIndex: theme.zIndex.dropdown }),
});
