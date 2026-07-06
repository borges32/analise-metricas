const path = require('path');
const CopyPlugin = require('copy-webpack-plugin');

const PLUGIN_ID = 'lgtm-analitico-app';

// Pacotes fornecidos pelo Grafana em runtime (não empacotar).
const GRAFANA_EXTERNALS = [
  'react',
  'react-dom',
  'react-router-dom',
  'rxjs',
  '@grafana/data',
  '@grafana/ui',
  '@grafana/runtime',
  '@emotion/css',
  '@emotion/react',
  'lodash',
];

module.exports = (env, argv) => {
  const isProd = argv.mode === 'production';
  return {
    target: 'web',
    mode: isProd ? 'production' : 'development',
    devtool: isProd ? false : 'source-map',
    entry: './src/module.tsx',
    output: {
      path: path.resolve(__dirname, 'dist'),
      filename: 'module.js',
      libraryTarget: 'amd',
      publicPath: `public/plugins/${PLUGIN_ID}/`,
      clean: true,
    },
    // Externals como AMD: o loader de plugins do Grafana resolve esses módulos.
    externalsType: 'amd',
    externals: GRAFANA_EXTERNALS,
    resolve: {
      extensions: ['.ts', '.tsx', '.js', '.jsx'],
    },
    module: {
      rules: [
        {
          test: /\.[jt]sx?$/,
          exclude: /node_modules/,
          use: {
            loader: 'babel-loader',
            options: {
              presets: [
                ['@babel/preset-env', { targets: 'defaults', modules: false }],
                ['@babel/preset-react', { runtime: 'classic' }],
                '@babel/preset-typescript',
              ],
            },
          },
        },
      ],
    },
    plugins: [
      new CopyPlugin({
        patterns: [
          { from: 'src/plugin.json', to: '.' },
          { from: 'src/img', to: 'img' },
          { from: 'README.md', to: '.', noErrorOnMissing: true },
        ],
      }),
    ],
  };
};
