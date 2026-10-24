import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from memory import Store, entries, MAX_FILE_BYTES, PART_BODY_BYTES, resolve_root


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # macOS /var aliases and Windows 8.3 temp paths must compare canonically.
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root, 'session-A')
        self.data = {'kind': 'dialogue', 'key': 'event-1', 'title': '记忆规则',
                     'source': 'session:session-A', 'content': '用户：压缩后只读摘要\n助手：按需检索'}

    def tearDown(self):
        self.temp.cleanup()

    def test_archive_txt_and_idempotency(self):
        first = self.store.archive(self.data)
        self.assertEqual(self.store.archive(self.data)['status'], 'unchanged')
        self.assertIn('对话_', Path(first['path']).name)
        self.assertIn('用户：', Path(first['path']).read_text(encoding='utf-8'))

    def test_conflicting_key_rejected(self):
        self.store.archive(self.data)
        with self.assertRaises(ValueError):
            self.store.archive({**self.data, 'content': '不同版本'})

    def test_learning_separate(self):
        result = self.store.archive({**self.data, 'kind': 'learning'})
        self.assertEqual(Path(result['path']).parent.name, 'learning')
        self.assertTrue(Path(result['path']).name.startswith('学习_'))

    def test_checkpoint_and_restore(self):
        self.store.archive(self.data)
        data = {'summary': '目标：持久化；下一步：测试。', 'covered_through': 'event-1'}
        checkpoint = self.store.checkpoint(data)
        self.assertEqual(self.store.restore()['summary'], data['summary'])
        self.assertEqual(self.store.checkpoint(data)['checkpoint_id'], checkpoint['checkpoint_id'])
        self.assertEqual(self.store.checkpoint(data)['status'], 'unchanged')

    def test_no_checkpoint(self):
        self.assertEqual(self.store.restore()['status'], 'no_checkpoint')

    def test_invalid_checkpoint(self):
        with self.assertRaises(ValueError):
            self.store.checkpoint({'summary': 'a', 'covered_through': 'absent'})
        with self.assertRaises(ValueError):
            self.store.checkpoint({'summary': 'a' * 2401, 'covered_through': 'absent'})

    def test_session_isolation(self):
        record = self.store.archive(self.data)
        other = Store(self.root, 'session-B')
        self.assertEqual(other.search('压缩')['hits'], [])
        self.assertEqual(len(other.search('压缩', all_sessions=True)['hits']), 1)
        with self.assertRaises(ValueError):
            other.read(record['path'])
        self.assertEqual(other.restore()['status'], 'no_checkpoint')

    def test_bounded_search(self):
        self.store.archive({**self.data, 'content': '压缩\\\"' * 8000})
        result = self.store.search('压缩', budget=800)
        self.assertTrue(result['hits'])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 800)

    def test_path_escape(self):
        with self.assertRaises(ValueError):
            self.store.read('../escape.txt')

    def test_redaction(self):
        result = self.store.archive({**self.data, 'content': 'SESSDATA=secret123; Bearer abcdefg'})
        text = Path(result['path']).read_text(encoding='utf-8')
        self.assertNotIn('secret123', text)
        self.assertNotIn('abcdefg', text)

    def test_lock_rejects_parallel_writer(self):
        with self.store.lock():
            with self.assertRaises(ValueError):
                self.store.archive(self.data)

    def test_checkpoint_history_preserved(self):
        self.store.archive(self.data)
        for summary in ('目标一', '目标二'):
            self.store.checkpoint({'summary': summary, 'covered_through': 'event-1'})
        self.assertEqual(len(list(self.root.rglob('续接_*.txt'))), 2)
        self.assertEqual(self.store.restore()['summary'], '目标二')

    def test_cli_archive_checkpoint_restore(self):
        script = Path(__file__).with_name('memory.py')
        prefix = [sys.executable, '-B', str(script), '--root', str(self.root / 'cli'), '--session', 'cli-session']
        record = self.root / 'input.json'
        record.write_text(json.dumps(self.data), encoding='utf-8')
        result = subprocess.run(prefix + ['archive', '--input', str(record)], capture_output=True, text=True, encoding='utf-8', check=True)
        self.assertEqual(json.loads(result.stdout)['status'], 'saved')
        record.write_text(json.dumps({'summary': 'next task', 'covered_through': 'event-1'}), encoding='utf-8')
        subprocess.run(prefix + ['checkpoint', '--input', str(record)], capture_output=True, check=True)
        result = subprocess.run(prefix + ['restore'], capture_output=True, text=True, encoding='utf-8', check=True)
        self.assertEqual(json.loads(result.stdout)['summary'], 'next task')

    def test_tiny_search_budget_rejected(self):
        with self.assertRaises(ValueError):
            self.store.search('test', budget=1)

    def test_small_records_share_daily_volume(self):
        paths = []
        for index in range(25):
            result = self.store.archive({**self.data, 'key': f'msg-{index}'})
            paths.append(result['path'])
        self.assertEqual(len(set(paths)), 1)
        self.assertEqual(len(list(entries(Path(paths[0])))), 25)
        self.assertEqual(self.store.archive({**self.data, 'key': 'msg-24'})['status'], 'unchanged')

    def test_utf8_split_preserves_all_content(self):
        body = '鲸鱼🐋记忆\n' * 40000
        result = self.store.archive({**self.data, 'content': body})
        files = list((self.root / 'dialogue').glob('*.txt'))
        self.assertGreater(result['parts'], 1)
        self.assertTrue(all(path.stat().st_size <= MAX_FILE_BYTES for path in files))
        parts = [item for path in files for item in entries(path)]
        rebuilt = ''.join(text for meta, text in sorted(parts, key=lambda item: item[0]['part']))
        self.assertEqual(rebuilt, body)
        self.assertEqual(self.store.archive({**self.data, 'content': body})['status'], 'unchanged')

    def test_rollover_by_encoded_byte_size(self):
        for index in range(3):
            self.store.archive({**self.data, 'key': f'large-{index}', 'content': '中' * 50000})
        files = list((self.root / 'dialogue').glob('*.txt'))
        self.assertEqual(len(files), 3)
        self.assertTrue(all(path.stat().st_size <= MAX_FILE_BYTES for path in files))

    def test_file_limit_includes_metadata(self):
        self.store.archive({**self.data, 'content': 'a' * PART_BODY_BYTES})
        self.assertTrue(all(path.stat().st_size <= MAX_FILE_BYTES for path in self.root.rglob('*.txt')))

    def test_read_paging_long_single_line(self):
        result = self.store.archive({**self.data, 'content': 'abcdefghijkl' * 1000})
        first = self.store.read(result['path'])
        self.assertEqual(len(first['text']), 2000)
        second = self.store.read(result['path'], offset=first['next_offset'])
        self.assertEqual(second['text'], ('abcdefghijkl' * 1000)[2000:4000])

    def test_search_paging(self):
        from unittest.mock import patch
        for index in range(5):
            self.store.archive({**self.data, 'key': f'page-{index}'})
        with patch('memory.MAX_SCAN_FILES', 2):
            first = self.store.search('压缩')
            self.assertTrue(first['more_records'])
            self.assertEqual(first['scanned_records'], 2)
            second = self.store.search('压缩', cursor=first['next_cursor'])
            self.assertFalse({hit['key'] for hit in first['hits']} & {hit['key'] for hit in second['hits']})

    def test_read_specific_record_in_shared_volume(self):
        first = self.store.archive(self.data)
        self.store.archive({**self.data, 'key': 'different', 'content': '另一个记录'})
        result = self.store.read(first['path'], record_id=first['id'])
        self.assertEqual(result['text'], self.data['content'])

    def test_oversized_legacy_file_not_read(self):
        path = self.root / 'dialogue' / 'legacy.txt'
        path.parent.mkdir()
        path.write_text('x' * (MAX_FILE_BYTES + 1), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.store.read(path)
        result = self.store.search('x')
        self.assertEqual(result['skipped_oversized'], 1)

    def test_oversized_metadata_rejected_without_partial_archive(self):
        with self.assertRaises(ValueError):
            self.store.archive({**self.data, 'title': '长' * 10000})
        self.assertEqual(list(self.root.rglob('*.txt')), [])

    def test_root_resolution_priority(self):
        from unittest.mock import patch
        config = self.root / 'memory.config.json'
        config.write_text(json.dumps({'root': './portable-data'}), encoding='utf-8')
        with patch.dict('os.environ', {'DSH_TOKEN_MEMORY_ROOT': str(self.root / 'env')}, clear=True):
            self.assertEqual(resolve_root(self.root / 'explicit', config), self.root / 'explicit')
            self.assertEqual(resolve_root(config_path=config), self.root / 'env')
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(resolve_root(config_path=config), self.root / 'portable-data')
            self.assertFalse((self.root / 'portable-data').exists())

    def test_dsh_home_default(self):
        from unittest.mock import patch
        with patch.dict('os.environ', {'DSH_HOME': str(self.root / 'custom-host')}, clear=True):
            self.assertEqual(resolve_root(config_path=self.root / 'absent.json'), self.root / 'custom-host' / 'token-memory')

    def test_user_home_fallback(self):
        from unittest.mock import patch
        with patch.dict('os.environ', {}, clear=True), patch('memory.Path.home', return_value=self.root):
            self.assertEqual(resolve_root(config_path=self.root / 'absent.json'), self.root / '.dsh' / 'token-memory')

    def test_cli_first_run_in_new_folder(self):
        script = Path(__file__).with_name('memory.py')
        prefix = [sys.executable, '-B', str(script), '--root', str(self.root / '空目录' / 'memory')]
        result = subprocess.run(prefix + ['paths'], capture_output=True, text=True, encoding='utf-8', check=True)
        self.assertFalse(json.loads(result.stdout)['initialized'])
        self.assertFalse((self.root / '空目录').exists())
        result = subprocess.run(prefix + ['init'], capture_output=True, text=True, encoding='utf-8', check=True)
        self.assertTrue(json.loads(result.stdout)['initialized'])
        for folder in ('dialogue', 'learning', 'checkpoints'):
            self.assertTrue((self.root / '空目录' / 'memory' / folder).is_dir())

    def test_installer_portable_and_preserves_private_config(self):
        from install import install, FILES
        target = self.root / '可移动技能' / 'dsh-token-memory'
        result = install(target)
        self.assertEqual(result['status'], 'installed')
        self.assertTrue(all((target / name).is_file() for name in FILES))
        config = target / 'memory.config.json'
        config.write_text(json.dumps({'root': './private-store'}), encoding='utf-8')
        with self.assertRaises(ValueError):
            install(target)
        result = install(target, upgrade=True)
        self.assertTrue(Path(result['backup']).is_file())
        self.assertEqual(json.loads(config.read_text(encoding='utf-8'))['root'], './private-store')
        self.assertFalse((target / 'private-store').exists())


if __name__ == '__main__':
    unittest.main()
