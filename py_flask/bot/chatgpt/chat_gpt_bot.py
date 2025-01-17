# encoding:utf-8

from bot.bot import Bot
from common.log import logger
from common.token_bucket import TokenBucket
from common.expired_dict import ExpiredDict
import openai
import time


# OpenAI对话模型API (可用)
class ChatGPTBot(Bot):

    def __init__(self, config_parser):
        logger.info("bot is starting...")
        openai.api_key = config_parser.api_key
        self._session = Session(config_parser)

        self._enable_rate_limit = False
        if config_parser.rate_limit_chatgpt > 0:
            self._enable_rate_limit = True
            self._tb4chatgpt = TokenBucket(config_parser.rate_limit_chatgpt)
        if len(config_parser.clear_memory_commands) > 0:
            self._clear_memory_commands = config_parser.clear_memory_commands
        if len(config_parser.clear_all_memory_commands) > 0:
            self._clear_all_memory_commands = config_parser.clear_all_memory_commands

    def reply(self, query, context=None):
        # acquire reply content
        if not context.get('type') or context.get('type') == 'TEXT':
            logger.info("[OPEN_AI] query={}".format(query))
            session_id = context.get('session_id')

            if query == self._clear_memory_commands:
                self._session.clear_session(session_id)
                return '会话已清除'
            if query == self._clear_all_memory_commands:
                self._session.clear_all_session()
                return '所有人会话历史已清除'

            session = self._session.build_session_query(query, session_id)
            logger.debug("[OPEN_AI] session query={}".format(session))

            # if context.get('stream'):
            #     # reply in stream
            #     return self.reply_text_stream(query, new_query, session_id)

            reply_content = self.reply_text(session, session_id, 0)
            logger.debug(
                "[OPEN_AI] new_query={}, session_id={}, reply_cont={}".format(
                    session, session_id, reply_content["content"]))
            if reply_content["completion_tokens"] > 0:
                self._session.save_session(reply_content["content"],
                                           session_id,
                                           reply_content["total_tokens"])
            return reply_content["content"]

    def reply_text(self, session, session_id, retry_count=0) -> dict:
        '''
        call openai's ChatCompletion to get the answer
        :param session: a conversation session
        :param session_id: session id
        :param retry_count: retry count
        :return: {}
        '''
        try:
            if self._enable_rate_limit and not self._tb4chatgpt.get_token():
                return {"completion_tokens": 0, "content": "提问太快啦，请休息一下再问我吧"}
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",  # 对话模型的名称
                messages=session,
                temperature=0.6,  # 值在[0,1]之间，越大表示回复越具有不确定性
                #max_tokens=4096,  # 回复最大的字符数
                top_p=1,
                frequency_penalty=0.0,  # [-2,2]之间，该值越大则更倾向于产生不同的内容
                presence_penalty=0.0,  # [-2,2]之间，该值越大则更倾向于产生不同的内容
            )
            # logger.info("[ChatGPT] reply={}, total_tokens={}".format(response.choices[0]['message']['content'], response["usage"]["total_tokens"]))
            return {
                "total_tokens": response["usage"]["total_tokens"],
                "completion_tokens": response["usage"]["completion_tokens"],
                "content": response.choices[0]['message']['content']
            }
        except openai.error.RateLimitError as e:
            # rate limit exception
            logger.warn(e)
            if retry_count < 1:
                time.sleep(5)
                logger.warn(
                    "[OPEN_AI] RateLimit exceed, 第{}次重试".format(retry_count +
                                                                1))
                return self.reply_text(session, session_id, retry_count + 1)
            else:
                return {"completion_tokens": 0, "content": "提问太快啦，请休息一下再问我吧"}
        except openai.error.APIConnectionError as e:
            # api connection exception
            logger.warn(e)
            logger.warn("[OPEN_AI] APIConnection failed")
            return {"completion_tokens": 0, "content": "我连接不到你的网络"}
        except openai.error.Timeout as e:
            logger.warn(e)
            logger.warn("[OPEN_AI] Timeout")
            return {"completion_tokens": 0, "content": "我没有收到你的消息"}
        except Exception as e:
            # unknown exception
            logger.exception(e)
            Session.clear_session(session_id)
            return {"completion_tokens": 0, "content": "请再问我一次吧"}


class Session(object):

    def __init__(self, config_parser):
        logger.info("Session init...")
        self._all_sessions = dict()
        if config_parser.expires_in_seconds > 0:
            self._all_sessions = ExpiredDict(config_parser.expires_in_seconds)
        self._max_tokens = config_parser.conversation_max_tokens
        if self._max_tokens <= 0:
            self._max_tokens = 1024
        self._character_desc = config_parser.character_desc

    def build_session_query(self, query, session_id):
        '''
        build query with conversation history
        e.g.  [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Who won the world series in 2020?"},
            {"role": "assistant", "content": "The Los Angeles Dodgers won the World Series in 2020."},
            {"role": "user", "content": "Where was it played?"}
        ]
        :param query: query content
        :param session_id: session id
        :return: query content with conversaction
        '''
        session = self._all_sessions.get(session_id, [])
        if len(session) == 0:
            system_prompt = self._character_desc
            system_item = {'role': 'system', 'content': system_prompt}
            session.append(system_item)
            self._all_sessions[session_id] = session
        user_item = {'role': 'user', 'content': query}
        session.append(user_item)
        return session

    def save_session(self, answer, session_id, total_tokens):
        session = self._all_sessions.get(session_id)
        if session:
            # append conversation
            gpt_item = {'role': 'assistant', 'content': answer}
            session.append(gpt_item)

        # discard exceed limit conversation
        self.discard_exceed_conversation(session, self._max_tokens,
                                         total_tokens)

    def discard_exceed_conversation(self, session, max_tokens, total_tokens):
        dec_tokens = int(total_tokens)
        logger.debug("prompt tokens used={},max_tokens={}".format(dec_tokens,max_tokens))
        while dec_tokens > max_tokens:
            # pop first conversation
            if len(session) > 3:
                session.pop(1)
                session.pop(1)
            else:
                break
            dec_tokens = dec_tokens - max_tokens

    def clear_session(self, session_id):
        self._all_sessions[session_id] = []

    def clear_all_session(self):
        self._all_sessions.clear()
