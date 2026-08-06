#include "WdnddsString.hpp"

#include <cstring>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <string>

#include <fastrtps/rtps/common/SerializedPayload.h>


namespace wdndds
{

WdnddsStringTopicDataType::WdnddsStringTopicDataType()
{
    setName("wdndds::string");
    m_typeSize = sizeof(uint32_t) + MAX_STRING_SIZE;
    m_isGetKeyDefined = false;
}

WdnddsStringTopicDataType::~WdnddsStringTopicDataType() = default;

bool WdnddsStringTopicDataType::serialize(
        void* data,
        eprosima::fastrtps::rtps::SerializedPayload_t* payload)
{
    if (data == nullptr || payload == nullptr)
    {
        return false;
    }
    const std::string* str = static_cast<std::string*>(data);
    const uint32_t len = static_cast<uint32_t>(str->size());
    if (len > MAX_STRING_SIZE)
    {
        return false;
    }

    const uint32_t total = sizeof(uint32_t) + len;
    payload->reserve(total);
    std::memcpy(payload->data, &len, sizeof(uint32_t));
    std::memcpy(payload->data + sizeof(uint32_t), str->data(), len);
    payload->length = total;
    return true;
}

bool WdnddsStringTopicDataType::deserialize(
        eprosima::fastrtps::rtps::SerializedPayload_t* payload,
        void* data)
{
    if (data == nullptr || payload == nullptr)
    {
        return false;
    }
    if (payload->length < sizeof(uint32_t))
    {
        return false;
    }
    uint32_t len = 0;
    std::memcpy(&len, payload->data, sizeof(uint32_t));
    if (len > MAX_STRING_SIZE || sizeof(uint32_t) + len > payload->length)
    {
        return false;
    }
    auto* str = static_cast<std::string*>(data);
    str->assign(reinterpret_cast<const char*>(payload->data + sizeof(uint32_t)), len);
    return true;
}

std::function<uint32_t()> WdnddsStringTopicDataType::getSerializedSizeProvider(
        void* data)
{
    return [data]() -> uint32_t {
        const std::string* str = static_cast<std::string*>(data);
        return static_cast<uint32_t>(sizeof(uint32_t) + str->size());
    };
}

void* WdnddsStringTopicDataType::createData()
{
    return new std::string();
}

void WdnddsStringTopicDataType::deleteData(
        void* data)
{
    delete static_cast<std::string*>(data);
}

bool WdnddsStringTopicDataType::getKey(
        void* /*data*/,
        eprosima::fastrtps::rtps::InstanceHandle_t* /*ihandle*/,
        bool /*force_md5*/)
{
    // Strings have NO_KEY semantics.
    return false;
}

}  // namespace wdndds
