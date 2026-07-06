import React from 'react';
import { AppRootProps } from '@grafana/data';

import { AppJsonData } from '../types';
import { AnalysisPage } from './AnalysisPage';

export function App(props: AppRootProps<AppJsonData>) {
  const uid = props.meta.jsonData?.datasourceUid;
  return <AnalysisPage datasourceUid={uid} />;
}
